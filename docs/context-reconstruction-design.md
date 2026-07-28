# Context Reconstruction — Design & Handoff

**Status:** Design (approved direction, Stage 1 not yet specced into a plan)
**Datum:** 2026-07-28
**Branch:** `feat/context-reconstruction`
**Scope:** Backend (`agent_scoped.py`), `mc` CLI, bridge adapters, `docker/shared/poll.sh`

---

## 0. What this document is

A session on 2026-07-27/28 asked a question MC had never asked explicitly: **what happens
to an agent's context when it comes back, and what *should* happen?**

The trigger was a diagram making the rounds ("How an agent (re)starts") showing
four return paths — cold boot / `--continue` / context handoff / crash-respawn — each
annotated with *"what survives"*. That framing turned out to be a useful lens on MC, not
because the techniques were new, but because MC has **all four mechanisms and no explicit
model tying them together**.

This document records:

1. A **verified** current-state analysis (§2) — everything with `file:line`, produced by
   four parallel read-only agents and spot-checked by hand.
2. The **target model** and the reasoning behind each fork (§3).
3. A design that was **considered and rejected**, with why (§4) — so the next session does
   not re-litigate it.
4. A **staged plan** (§5–§7), of which Stage 1 is specced in detail.
5. Open questions and the re-entry point (§8–§9).

> **Confidence note.** Findings marked ✅ were verified directly against the code in this
> repo. Findings marked ⚠️ come from a single agent report and were *not* independently
> re-checked. Nothing here is inferred from memory or assumption — where something could
> not be verified, it says so.

---

## 1. Problem in one line

An MC agent's working context lives **only in the RAM of a running TUI process**. Every
restart path throws it away, the recovery that follows differs per harness, and the agent
has **no way to read back its own conversation** — so it cannot rebuild what it lost.

---

## 2. Current state (verified)

### 2.1 The four modes, as they exist in MC today

| Diagram mode | MC reality | What actually survives |
|---|---|---|
| Cold boot (fresh) | `docker restart` / `--force-recreate` | Bind-mounts, full Postgres state, tokens re-fetched via bootstrap |
| `--continue` | **Does not exist** | Only while the tmux process lives (history = RAM) |
| Context handoff | MEMORY.md, reflection→lessons, progress comments | Yes, but injected unevenly |
| Crash-respawn | Docker `unless-stopped` + launchd + 4-tier watchdog | Task status, comm_v2 messages (redelivered) |

### 2.2 Findings

#### F1 — There is no session resume anywhere in MC ✅

Grep across `backend/`, `scripts/`, `docker/` for `--continue` / `--resume` / `--session-id`:
**zero hits.** Agents start blank every time:

- `docker/mc-claude-agent/start-claude.sh:48` — `exec claude --dangerously-skip-permissions --append-system-prompt <SOUL/CARD.md>`
- `docker/mc-kimi-agent/start-kimi.sh:42` — `exec kimi --auto`

What MC calls a "session" is a **long-lived interactive TUI in a persistent tmux window**
(window 0, session name = agent slug). Dispatch types prompts into it via
`tmux paste-buffer`. History survives exactly as long as that process does.

#### F2 — Session JSONL files are a write-only backup ✅

`/home/agent/.claude` is bind-mounted per agent
(`docker/docker-compose.agents.yml:92,117,144,…` → `~/.mc/agents/<slug>/claude-config`).
Claude Code writes `projects/**/*.jsonl` transcripts there. They persist across recreate.

**They are never read back** — because of F1, there is no resume path that would load them.
After a `/clear` or restart a *new* transcript begins; the old one is orphaned on disk.

Two consequences:
- The disk persistence buys us **nothing** for context continuity today.
- The files are written forever, never read, **never rotated** → slow disk leak.

#### F3 — An agent cannot read its own thread ✅ (load-bearing)

This is the keystone finding. Verified by hand:

- Full thread read exists: `backend/app/routers/tasks.py:2632` `GET /tasks/{task_id}/thread`
  — seq-paged, supports `since_seq` / `before_seq` / `limit`, "load older" pagination.
  **Gated `current_user=Depends(require_user)`** → operator/UI only.
- `backend/app/routers/agent_scoped.py` has thread **writes** only:
  `mc ask` (:1352), `mc msg` (:1486). There is **no** agent-side thread read endpoint.
- What the agent gets instead is a **delta**: `GET /agent/me/poll` (`agents.py:2670`)
  returns only unacked messages via `_unacked_thread_messages` (`agents.py:2611-2623`,
  collected at `:2626`) → surfaced as `mc inbox`.

So: the richest record of what an agent did and decided — its own thread — is readable by
the operator and **not by the agent**. After any context loss it cannot look up the
conversation it itself conducted.

> ⭐ **Correction to an older note.** A memory entry from the Kimi briefing (2026-07-23)
> said *"user-seitige Thread-LESE-API fehlt"*. That is **outdated** — the user-side API
> exists (`tasks.py:2632`). What is missing is the **agent-side** one.

#### F4 — Recovery coverage is uneven across the fleet ✅ (load-bearing)

Three independent cold-boot recovery mechanisms exist:

1. **poll.sh startup recovery** — on first poll, if backend says `state=working` but local
   context is missing → `GET /me/active-task-recovery` (`agents.py:3018`) → re-paste.
   (`docker/shared/poll.sh:783-816`, `recover_task` at `:402-418`.) Read-only, mutates no
   status.
2. **Bootstrap recovery recap** — the container entrypoint's
   `GET /api/v1/internal/bootstrap` call is the "my process came up fresh" signal.
   `internal.py:271` → `_maybe_post_bootstrap_recovery_recap` (`:286-359`) posts a compact
   `recovery_recap` TaskComment when `agent.current_task_id` still points at an
   `in_progress` task. Redis dedup 600 s + shared cooldown.
3. **Unread thread messages** — DB-persisted cursor (`last_acked_seq`), redelivered on next
   poll. Runtime-agnostic.

Coverage is **not uniform**:

| Agent form | #1 active-task-recovery | #2 bootstrap recap | #3 unread msgs |
|---|---|---|---|
| Docker cli-bridge | ✅ | ✅ **(only here)** | ✅ |
| Boss-Host | ✅ (own `boss-host/poll.sh:97-112`) | ❌ | ✅ |
| Kimi-Host ⚠️ | ✅ (shared poll.sh) | ❌ | ✅ |
| **Hermes** | ❌ | ❌ | ✅ |
| **Grok** | ❌ | ❌ | ✅ |

**Root cause of the #2 column:** only the Docker entrypoint calls
`GET /internal/bootstrap` (`docker/mc-claude-agent/entrypoint.sh:21`,
`docker/mc-kimi-agent/entrypoint.sh:22`). Every host form sources `agent.env` instead
(`docker/boss-host/entrypoint.sh:9,24-30`; `docker/hermes/entrypoint.sh:15-21`), so
`_maybe_post_bootstrap_recovery_recap` never fires for them.

**Root cause of the #1 column:** Hermes and Grok follow the *bridge* pattern with no
`poll.sh` at all — `docker/hermes/entrypoint.sh:47-49` runs the binary in a bare
`while true` loop; delivery goes through `scripts/hermes-bridge.py` /
`scripts/grok-bridge.py` (nudge + pull, ADR-071).

Verified by hand: `grep -n "active-task-recovery\|recover_task\|recovery"` on both bridge
scripts returns **zero hits**.

> **The sharp case:** if Grok or Hermes dies mid-task, its only thread back is an unread
> thread message. There is no task-prompt redelivery.

⚠️ *Kimi-Host lives on branch `feat/kimi-harness` (commit `98177a04`), not on `main`.
Grok's entrypoint itself was not read — only its plist and bridge script.*

#### F5 — Related: hang detection is also cli-bridge-only ⚠️

- The stuck-in-progress detector (ADR-046) is gated on
  `agent.agent_runtime == "cli-bridge"` (`task_runner.py:1436`, logic `:1383-1560`) —
  because only that runtime stamps `last_task_activity_at` during a turn.
- Orphan recovery (`watchdog/task_monitor.py:1294-1362`) needs `last_seen_at` **also**
  stale (> 30 min).
- No compose healthcheck exists (`watchdog/health_checks.py:154-156`, "TODO Phase 31").

→ A **host** agent whose wrapper is alive but whose LLM turn died silently falls through
both nets. Combined with F4 this means host bridge agents have **neither hang detection
nor task resumption**.

*(This is documented and intentional in the ADR-046 design — blocking a runtime that
doesn't emit a working-heartbeat would violate its prime directive. It is listed here as
context, not as a defect.)*

#### F6 — MEMORY.md never reaches the prompt ⚠️

- Agent memory is a DB column (`agent.memory_md`); the agent reads it via
  `GET /agent/me/memory` (`agent_scoped.py:530`) and **writes it itself** via
  `PATCH /agent/me/memory` (`:468`). It is rendered to
  `~/.claude/MEMORY.md` in the container (`docker_agent_sync.py:272-274`).
- **Only SOUL.md is injected** via `--append-system-prompt` (`docker_agent_sync.py:241`).

→ The agent's own memory sits on disk, invisible, unless the model spontaneously decides
to `cat` it. Nothing points at it.

#### F7 — Double injection on cold boot ⚠️ (resolved contradiction — read this)

Two agent reports contradicted each other here; the contradiction was resolved and the
finding **downgraded**. Recording the resolution so it is not re-opened:

- `_maybe_post_bootstrap_recovery_recap` (`internal.py:286-359`) **is** live-wired.
- `session_monitor._build_recovery_recap` (`watchdog/session_monitor.py:99-156`) **is**
  parked (callers removed in Phase 29). *These are two different functions* — the first
  report conflated them.
- On cold boot with an open `in_progress` task, **both** happen:
  (a) compact recap TaskComment from the backend, and
  (b) a tmux paste from `poll.sh recover_task` (`:402-418`).
- Worse, the recovery snippet appears **twice**: `build_agent_task_prompt`
  (`dispatch.py:578-592`) internally calls the same `build_recovery_context`
  (`task_context_builder.py:883-951`) and embeds it in the dispatch message. No dedup
  between the two paths.

**But it is not the 8000-char bomb** `CLAUDE.local.md` warns about. The dispatch message
builder budgets hard: TARGET 2000 / WARN 2500 / **HARD 4000** chars
(`dispatch_message_builder.py:62-64`), with `_assemble_with_budget` (`:1173-1197`) dropping
optional sections until it fits. The pasted prompt is a *budget-capped continue prompt*,
~2–4k chars, not the original dispatch.

→ **Redundancy worth cleaning up, not an active bug.** Note that the `CLAUDE.local.md` rule
predates the budget system and should be re-worded when this is touched.

#### F8 — `TaskCheckpoint` is dormant, and `get_last_checkpoint` is likely dead ⚠️

- `models/checkpoint.py` (`state_summary`, `context_data`) — migration 0082 moved
  checkpoints to `progress` comments; `POST /checkpoint` returns **410**;
  `build_recovery_context` no longer reads the table.
- `task_context_builder.py:809-821` `get_last_checkpoint()` still searches for
  `TaskComment.comment_type == "checkpoint"` — a type migration 0082 renamed away.
  **Probably dead code.** Whether it is still called was not verified.

> ⚠️ **Do not "revive" this casually.** An earlier draft of this design proposed
> `mc checkpoint` writing `comment_type="checkpoint"` as an elegant way to bring the dead
> path back to life. That would **re-split what migration 0082 deliberately merged**.
> Before going that way, read 0082 and find out *why* it merged them.

#### F9 — comm_v2 is end-to-end at-least-once ✅ (good news, no action)

Verified by hand in `docker/shared/poll.sh`:

- `flush_msg_queue` (`:876-900`) pastes first, then calls `_record_ack` (`:799-811`)
  **only on `rc==0`** (paste verified by fingerprint in the pane). On gate-closed (`rc==2`)
  or verify failure: no ack, message stays queued.
- The ack is sent on the *next* poll via `build_acked_seq_param` (`:354-382`).
- Messages are persisted to disk **before** any paste (`queue_or_deliver` `:825-857`).
- In **nudge** mode — the fleet default (`compose_renderer:620`) — there are no local ack
  files at all; the ack happens server-side when the agent calls `mc inbox`
  (`POST /me/inbox/ack`), i.e. *after the agent has actually read it*.

`MSG_QUEUE_DIR` / `MSG_ACK_DIR` live under `/home/agent/` which is **not** mounted (only
`/home/agent/.claude` is) → wiped on `--force-recreate`. **Irrelevant for correctness:** the
backend cursor is the durable truth, and a lost ack means redelivery, never loss.

> ⚠️ One honest caveat from the analysis, and it matters for §3: *"pasted + verified"* is
> **not** proof that the LLM ingested the text — the fingerprint only proves it appeared as
> a submitted input line in the pane. `poll.sh:119-121` acknowledges this and hardens
> against false-positives with a unique seq+epoch token, but the gap is real.

---

## 3. Target model

### 3.1 Decision A — Reconstruct, don't preserve

**The conversation is a cache. The truth is Postgres + the Git workspace.**

An agent rebuilds its context on demand rather than trying to keep it alive.

*Why (the argument that decided it):* preserving is **harness-dependent** — only some CLIs
could ever support resume, and F1 shows none do today. Reconstructing is
**harness-independent**. With five different CLIs in the fleet, this is not a matter of
taste; it is the only model that can unify them.

Consequence: all four diagram modes collapse into a **single event** — "the context is
gone" — with a single answer.

**Known limitation, stated honestly:** reconstruction recovers *what happened*, not *what
was understood*. It cannot restore rejected approaches, dead ends already explored, or the
shape of a problem built up over 40 tool calls. For mechanical tasks this is cheap and
lossless enough. For long design tasks it is not. See §4.2.

### 3.2 Decision B — Pull, not push

The backend does **not** assemble a finished context package. It says *"you were on task X"*
and the agent digs to whatever depth it needs: `mc resume`, `mc thread`, `git log`,
`cat MEMORY.md`.

*Why:* consistent with the nudge+pull line already chosen for comm_v2 (ADR-071); token-thrifty
(fetch only what's needed); and it removes the "backend guesses what's relevant" problem
that produced the budget-truncation awkwardness in F7.

*Cost:* it requires the agent-side read API that F3 says does not exist. **That is Stage 1.**

### 3.3 Decision C — Reconstruction is a general primitive, not just a recovery path

If reconstruction is reliable, `/clear` stops being dangerous and becomes the **relief valve
for the context limit**: at high ctx%, an agent writes out its state, clears, and rebuilds —
without dropping the task. Restart then becomes just *a `/clear` the agent didn't trigger*.

MC today measures ctx% (scraped from the status line into the heartbeat,
`poll.sh:307-352`) but never acts on it. `/clear` is task-boundary-driven, never
size-driven.

**This is Stage 3 and it is conditional** — see §4.2 for the unresolved risk.

---

## 4. Rejected / deferred: the hard gate

An earlier version of this design made a **hard backend gate** the centrepiece:
`agent.context_state ∈ {fresh, stale}`; any context loss sets `stale`; all writes
(`mc done`, `mc msg`, status transitions) return **409** until `mc resume` is called.

It was rejected **for Stage 1–2** after review. Recording the reasoning so it is not
rebuilt by reflex:

### 4.1 Why the gate was deferred

| Objection | Detail |
|---|---|
| **It does not cover the agents it was meant to protect** | The backend can only observe a `/clear` where `poll.sh` reports one. Per F4, Hermes and Grok have no `poll.sh`. The gate's promised uniformity is not delivered — coverage stays exactly as uneven as today. |
| **A false `stale` is worse than a false `fresh`** | An agent with full context that gets wrongly blocked burns a turn on a `mc resume` it doesn't need, and gets confused mid-task. That is a regression. |
| **The cited precedent doesn't hold** | Forced reflection on `mc done` is a *schema requirement on a call the agent deliberately makes*. The gate is a state that fires unpredictably in the middle of work. Not the same thing. |
| **It solves an unmeasured problem** | The current-state analysis found *mechanisms*, not *failure rates*. Nobody has checked whether agents actually wake up context-less and blunder ahead. Building a blocking state machine into every agent's critical path on suspicion is backwards. |
| **It adds machinery to the critical path** | Deadlock escape + watchdog + operator override — all of it sits between an agent and its ability to finish work. |

**Replaced by:** instrumentation in Stage 2 (§6). Measure whether agents resume; build the
gate in Stage 3 only if the data demands it — and then target it where it demonstrably
tears.

*Note:* F9's caveat cuts **for** an eventual gate. If a paste cannot prove the LLM ingested
anything, then an agent-initiated `mc resume` call is the only real receipt we can get.
That argues the gate may eventually be right — but it should be earned with data.

### 4.2 Why the context valve (Decision C) is conditional

The valve is most attractive exactly where reconstruction is least faithful (§3.1): long,
design-heavy tasks. `mc checkpoint` is the proposed answer, but it relocates the problem —
quality now depends on the agent writing a *good* summary **under context pressure**,
precisely when it is least able to, with no verification that the checkpoint was adequate.
You find out it wasn't when the agent redoes discarded work.

**This tradeoff was not surfaced when the direction was originally chosen.** It needs a real
experiment on a long task before commitment.

---

## 5. Stage 1 — Make reconstruction possible

**Goal:** every agent *can* rebuild its context. Nothing is forced, nothing is blocked.
Independently valuable even if Stages 2–3 never happen.

**Risk: none.** Purely additive capability.

### 5.1 `GET /agent/me/thread` → `mc thread`

New endpoint in `backend/app/routers/agent_scoped.py`. Mirrors the semantics of
`tasks.py:2632` so behaviour is consistent between operator and agent views.

| Param | Type | Meaning |
|---|---|---|
| `task_id` | uuid, optional | defaults to the agent's current task |
| `limit` | int, 1–200, default 50 | page size |
| `before_seq` | int, optional | backward pagination (older) |
| `since_seq` | int, optional | forward delta |

Response: ascending `seq` order, `has_more_before` flag, same shape as the user endpoint.
A task with no thread reads as an **empty page**, never 404, and **never creates** the
thread (matching `tasks.py` behaviour).

**Two hard constraints:**

1. 🔴 **Reading must NOT touch the ack cursor.** This is the one real trap in Stage 1.
   If reading acked, an agent rebuilding its context after a restart would **swallow its own
   undelivered mail** — and comm_v2 would lose the at-least-once guarantee F9 just
   confirmed intact.
   **Rule: `mc inbox` acks (consume). `mc thread` never acks (look up).**
   This belongs in the test suite as an explicit assertion, not as a code comment.

2. **Own tasks only.** The agent may read threads of tasks assigned to it. No fleet-wide
   reading. Least privilege.

### 5.2 `GET /agent/me/resume` → `mc resume [task]`

Compact re-entry spine, ~600–800 chars. Read-only — **mutates no status** (same discipline
as `/me/active-task-recovery`, which was deliberately made read-only per ADR-024 to avoid
dispatch-loop risk).

Contents:

- Task ID / title / status
- Checklist with a `← CONTINUE HERE` marker (reuse `build_recovery_context`'s existing
  marker logic, `task_context_builder.py:883-951`)
- Workspace path + short `git` state
- Last ~5 thread events as one-liners
- An explicit pointer: *"full history: `mc thread`"*

Deliberately **not** a full reconstruction — depth is pulled, per Decision B.

*Relationship to the existing endpoint:* `/me/active-task-recovery` (`agents.py:3018`)
returns a full budget-capped dispatch prompt. Stage 1 **adds** `mc resume` alongside it.
Stage 2 switches `poll.sh` over and retires the double injection (F7).

### 5.3 Make MEMORY.md visible (F6)

**Pointer in SOUL.md, not full injection.** Full injection would burn MEMORY.md into every
turn and the file grows. A pointer makes the memory *findable* instead of invisible, and
stays consistent with Decision B.

*(Open: whether the pointer belongs in the SOUL template or in the `mc resume` payload —
arguably both. See §8.)*

### 5.4 Bridge parity for the read commands

- **Grok** — has shell `mc` (fixed in PR #150) → inherits both commands automatically once
  the endpoints exist.
- **Hermes** — MCP-only, no shell `mc`. Needs `mc_thread` and `mc_resume` MCP tools
  alongside the existing `mc_inbox` tool.

### 5.5 JSONL rotation (F2)

Transcripts are written forever and never read. Add rotation/pruning. Small, independent.

**Do not delete the mounts** — they remain useful for forensics. Just stop pretending they
are a recovery mechanism, and stop letting them grow unbounded.

### 5.6 Tests

- Endpoint tests for both new endpoints (pagination, empty thread, defaults).
- 🔴 **Cursor-untouched test**: call `mc thread` over a thread with unacked messages, assert
  `last_acked_seq` and `last_delivered_seq` are unchanged, then assert the next `mc inbox`
  still returns those messages.
- Authorization test: agent cannot read another agent's task thread.
- `mc resume` mutates nothing: assert task status, `current_task_id` and cursors unchanged.

---

## 6. Stage 2 — Make it happen, and measure

1. **SOUL instruction + wake-up wording** — *"if you don't know what you're working on:
   `mc resume` first, never guess."*
2. ⭐ **Instrumentation — the point of this stage.** Record, per context-loss event, whether
   `mc resume` was called **before the first write**. This is a cheap counter and it answers
   the question the rejected gate was guessing at: *do agents actually forget?*
3. **Fleet parity (F4)** — Hermes/Grok get restart detection + a resume wake-up through the
   adapter contract (ADR-071 + TCK). This is needed regardless of any gate.
4. **Remove the double injection (F7)** — `recover_task` pastes the wake-up, not the
   dispatch prompt. Re-word the stale `CLAUDE.local.md` rule while touching it.

**Live gate:** kill an agent mid-task; does it come back cleanly? Run it per harness — the
Kimi lesson (2026-07-25) applies: *a spike shows only ONE state; verify against the running
production container.*

## 7. Stage 3 — Conditional, data-driven

- Instrumentation shows agents resume reliably → **no gate needed.** We saved a state
  machine.
- Instrumentation shows they skip it → build the gate, targeted where it measurably tears.
- **Context valve** separately, opt-in per agent or task type first, tested on a genuinely
  long task, because of §4.2.

---

## 8. Open questions

1. **Migration 0082** — why were `checkpoint` and `progress` comment types merged? Answer
   before touching F8 either way (revive vs. delete `get_last_checkpoint`).
2. **Does `mc resume` default to the current task, or require an explicit ID?** Defaulting
   is friendlier; explicit is safer if `current_task_id` is stale after a hard crash — which
   F4 notes is exactly the failure mode that breaks recovery #2.
3. **MEMORY.md pointer placement** — SOUL template, `mc resume` payload, or both.
4. **Host-form recovery** — Boss-Host has its **own copy** of `poll.sh`
   (`docker/boss-host/poll.sh`, 436 lines) rather than sharing `docker/shared/poll.sh`.
   Stage 2 parity work should decide whether to converge them or keep them separate. Drift
   between the two is a standing risk.
5. **Should `mc thread` be readable for *completed* tasks?** Useful for "how did I solve this
   last time"; widens the authorization surface.

---

## 9. Re-entry for the next session

**Where we are:** direction approved (§3 Decisions A + B, staged per §5–§7). Stage 1 is
specced at the level above; it has **not** been turned into an implementation plan yet.

**Start here:**

1. Read this document, then `docs/decisions/071-w21-delivery-foundation.md` (adapter
   contract) and `backend/app/routers/agent_scoped.py` (where the new endpoints go).
2. Answer open question §8.2 (resume default) — it shapes the endpoint signature.
3. Invoke the `writing-plans` skill to turn §5 into an implementation plan.
4. TDD per `superpowers:test-driven-development`; the cursor-untouched test (§5.6) is the
   one that must exist **before** the endpoint.

**A worktree is already set up** for this branch (branched from `main`); see
`git worktree list`.

**Do not** start with the gate or `context_state`. §4 explains why; if you find yourself
reaching for it, the answer is instrumentation first.

---

## 10. Provenance

Current state produced 2026-07-27 by four parallel read-only agents (cold boot / continue /
handoff / crash-respawn) against branch `feat/ui-redesign-v3`, plus follow-ups that resolved
one inter-agent contradiction (F7) and closed three self-declared gaps (host-form coverage,
Kimi-Host location, ack timing). Findings marked ✅ were re-verified by hand; ⚠️ ones were
not.

Direction reviewed and revised on 2026-07-28: Decisions A and B kept, the hard gate demoted
to instrumentation, and the work staged (§4).
