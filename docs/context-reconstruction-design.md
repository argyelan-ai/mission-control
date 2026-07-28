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

> **Base note (2026-07-28, second pass).** The branch was originally cut from a *local*
> `main` that was 54 commits behind `origin/main` — comm_v2 was not in it at all, so none
> of the thread code referenced below existed on the working base. The branch has since been
> rebased onto `origin/main` (`5a63c678`) and **every `file:line` in §2 was re-checked against
> that base**. `tasks.py:2632` is exact; `mc ask` / `mc msg` are at `agent_scoped.py:1346` /
> `:1480` (the §2 draft said `:1352` / `:1480`, a 6-line drift from the `feat/ui-redesign-v3`
> analysis base).

---

## 1. Problem in one line

An MC agent's working context lives **only in the RAM of a running TUI process**. Every
restart path throws it away, the recovery that follows differs per harness, and the agent
has **no way to read back its own conversation** — so it can recover *the task* but not
*the reasoning*.

> Sharpened after F10. The first draft said "cannot rebuild what it lost", which is wrong:
> `mc recover` rebuilds the task and has since ADR-024. The gap is narrower and more
> specific — the thread, the agent's own richest record, is operator-readable and
> agent-invisible.

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

#### F8 — `TaskCheckpoint` is dormant, and `get_last_checkpoint` is dead ✅ (resolved 2026-07-28)

> **Resolved — open question §8.1 is closed.** Migration 0082
> (`0082_deprecate_checkpoint_comments.py`) is a one-shot data backfill:
> `UPDATE task_comments SET comment_type='progress' WHERE comment_type='checkpoint'`, with a
> deliberately irreversible no-op `downgrade()`. The *why* is **ADR-020 §A4**: three parallel
> progress-tracking systems (`TaskCheckpoint`, `comment_type='checkpoint'`,
> `TaskChecklistItem`) with overlapping semantics were consolidated onto `TaskChecklistItem`
> — 163 real usages vs. 8 for `TaskCheckpoint`.
>
> `get_last_checkpoint` (`task_context_builder.py:809`) has **zero call sites** repo-wide.
> The only non-definition reference is an unused `# noqa: F401` re-export shim
> (`dispatch.py:587`). And even if called it would always return `None` on any migrated DB.
> **Verdict: delete it plus the shim entry. Do not revive.** `build_recovery_context` is its
> documented replacement.
>
> Caveat worth knowing: `"checkpoint"` is *still* an accepted API value
> (`comment_types.py:19`) — 0082 added no CHECK constraint — so an agent could write a *new*
> checkpoint-typed comment today. Four consumers tolerate the value via `.in_([...])`
> alongside `"progress"`; none require it.
>
> Also surfaced: two ADR-020 rollout steps were never executed — delete `POST /checkpoint`
> after 2 releases (still a 410 shim at `agent_task_status.py:2473`) and `DROP TABLE
> task_checkpoints` after 3 weeks (still there, still read by a live `GET`). **Out of scope
> here** — but it is a real loose end someone should pick up.

Original finding, kept for the record:

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

#### F10 — ⭐ `mc recover` already exists, and SOUL already mandates it ✅ (2026-07-28, corrects §1)

**This finding removes roughly half of what §5 originally proposed to build.** It was missed
in the first pass because the analysis searched for *session* resume (`--continue`,
`--resume`) and found nothing — but MC's answer to "where was I?" was never a session flag.
It is a CLI verb, and it has been shipped since ADR-024:

| Piece | Location | State |
|---|---|---|
| Endpoint | `agents.py:3056` `GET /agent/me/active-task-recovery` | live, read-only, mutates no status, Redis-rate-limited 1/30 s per task |
| CLI verb | `commands.py:1665` `_cmd_recover`, registry `:2378` | live — prints the prompt, writes `/tmp/mc-context.env` with a fresh `dispatch_attempt_id` |
| SOUL rule | `SOUL.md.j2:79` and `:2935` ("Startup Check — `mc recover` first (ADR-024)") | live, instructs *every* agent to run it after a restart |
| poll.sh fallback | `poll.sh:404,418` | live — calls the same endpoint if the agent doesn't |

So the capability, the instruction, and the automatic fallback all already exist. §1's
"the agent has no way to rebuild what it lost" is **too strong**: it can rebuild *the task*.
What it still cannot rebuild is *the conversation* (F3) — and that part stands unchanged.

Three real gaps remain behind this:

1. **Hermes cannot execute the SOUL rule at all.** It is MCP-only; `scripts/mc-mcp.py`
   exposes 17 `mc_*` tools (`mc_inbox`, `mc_tasks`, …) and **`mc_recover` is not among
   them**. The SOUL instruction is literally unexecutable for it. Grok has shell `mc`
   (PR #150) so it inherits the verb — nothing verifies it ever runs it.
2. **`mc recover` returns the full budget-capped dispatch prompt** (up to 4000 chars),
   not the compact spine §5.2 wanted — and it is precisely one half of the F7 double
   injection.
3. **Nobody has ever measured whether agents actually run it.** Unchanged, and now the
   single most valuable thing to build.

#### F11 — ⭐ Hermes persists its own memory automatically, and MC cannot see it ✅ (2026-07-28)

Raised by the operator as a caution before building; it turned out to be the sharpest
constraint in the whole design. Hermes is not "an agent with a different CLI" — it has a
**second, independent memory system that MC neither writes nor reads**.

**MC's SOUL never reaches Hermes.** Hermes' binary loads `~/.hermes/SOUL.md` into the stable
identity slot of its system prompt (`hermes-agent/agent/prompt_builder.py:1796-1823`,
`system_prompt.py:153-162`). MC renders its own 43 KB SOUL to
`~/.mc/agents/hermes/…` (`docker_agent_sync.py:552-564`). Nothing copies one to the other —
verified across `docker/hermes/entrypoint.sh` (70 lines), `scripts/hermes-bridge.py` (1101
lines) and `hermes-config-patch.py`. The live `~/.hermes/SOUL.md` is 513 bytes of vendor
default text.
→ **F10's claim that "Hermes is ordered by its SOUL to run `mc recover`" is false.** The
instruction never arrives. Hermes' real instruction surface is the dispatch prompt built in
`hermes-bridge.py:250-271` plus `~/.hermes/skills/mission-control/SKILL.md` — and that
SKILL.md is **referenced once in the repo and generated nowhere**, i.e. untracked host state.

**The auto-memory loop** (verified in config and code):

| Mechanism | Evidence |
|---|---|
| every ~5 user turns a background fork digests the last 24 messages and may write memory | `turn_context.py:307-314` + `nudge_interval: 5` in `~/.hermes/config.yaml:367` |
| it writes without operator or task involvement | `background_review.py:6-7,167-168`, origin tag `"background_review"` `:698` |
| the store is capped at **2200 characters** | `~/.hermes/config.yaml:364` `memory_char_limit: 2200` |
| it is injected into **every** later system prompt | `system_prompt.py:426-435`, reloaded on each compression `:503-505` |
| context auto-compaction is on | `~/.hermes/config.yaml:359-360` `engine: compressor` |
| MC has no visibility: zero `memory_md` calls in `hermes-bridge.py` / `mc-mcp.py` | grep, no hits |

**Why this bites this design specifically.** A bridge paste *is* a user turn
(`hermes-bridge.py:274-282`). So anything we hand Hermes is eligible, within ~5 turns, to be
summarised into a 2200-char store that then prefixes every future task — **across task
boundaries, invisible to MC, with no rollback**. Two concrete consequences:

1. A 4000-char `mc recover` payload or an unbounded `mc thread` dump landing shortly before
   a review tick can **evict genuine long-term memory** and pin one task's context onto all
   later ones.
2. `mc thread` returns **third-party text** (operator and other agents). Making that eligible
   for automatic persistence into an identity-level prompt is a prompt-injection path *with
   persistence*, not merely a context path.

🔴 **And the obvious implementation is the trap.** `scripts/mc-mcp.py` has two clients:
`_api_agent` (agent token) and `_api` (`:36-44`) — which mints a **`role: admin` JWT valid
24 h**. Most read tools use `_api`. Writing `mc_thread` the easy way (`_api` against the
existing `tasks.py:2632`) would hand Hermes **fleet-wide thread read with admin rights**,
silently voiding the least-privilege rule in §5.1. Any Hermes tool here must use
`_api_agent` against the new agent-scoped endpoint, and a test must assert it.

*Unverified:* whether `hermes --yolo` auto-resumes the prior session from
`~/.hermes/state.db` (287 MB) on restart, and whether the background review is suppressed in
unattended mode. Both would change the picture and should be checked before any Hermes work.

#### F12 — Parallel work on the same tables, and where the boundary runs ✅ (2026-07-28)

The Telegram team-chat work (PRs #171–#178, all merged to `main`; only the template-only
PR #181 is open) touched the comm_v2 read path. Coordinated directly with its author. Three
consequences for this design, each verified here against `origin/main`:

1. ⭐ **The cursor trap is independently confirmed** — and it is worse than "don't ack".
   `_get_or_create_thread_cursor` (`agents.py:2552`) now returns **`(cursor, created)`**, and
   `_resolve_agent_threads_with_cursors` returns **`(list, created_any)`** (`:2646`). The
   reason is a real bug fixed in PR #150: the old code detected fresh cursors via
   `cursor in session.new`, but any later SELECT in the same request autoflushes the pending
   insert out of `session.new` — so the cursor creation rolled back, and threads of finished
   tasks were **re-fast-forwarded on every poll**, losing every message that arrived in
   between, permanently.
   → **Anyone building a second read path must carry the `created_any` signal.** Relying on
   `session.new` reintroduces the bug. Regression cover: `test_message_delivery.py`,
   `test_inbox_pull.py` — run both.

2. **DM threads now exist and are in scope.** `Thread.kind == "dm"` with `Thread.agent_id`
   (`agents.py:2534-2541`). Their pair carries **`None` instead of a Task**, deliberately —
   a DM has no "finished history" to fast-forward past. Any code reading
   `thread_task.status` must null-guard it (`:2642` already does via `bool(thread_task)`).
   *Open for this design:* should `mc thread` also read the agent's DM? It is arguably
   context the agent should be able to look up. Deferred — v1 is task threads.

3. **Boundary agreed.** `scripts/mc-cli/` and `scripts/mc-mcp.py` are untouched by that work
   → free. `agents.py`'s read path is theirs → **this design must not modify it.**

That third point is a design constraint, not just etiquette, and it happens to be free:
**`mc thread` reads one task's thread by id** — resolve the task, then `task.thread_id` —
so it never needs `_message_threads_for_agent` or the cursor helper at all. The new endpoint
lives in `agent_scoped.py`. Sidestepping that machinery is what makes constraint 🔴 in §5.1
enforceable rather than merely intended.

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

> **Scope correction, 2026-07-28 (second pass).** F10 removed the largest item in this
> stage: `mc resume` was going to duplicate `mc recover`, which already exists and is
> already mandated by SOUL. What remains:
>
> | # | Item | Status |
> |---|---|---|
> | 5.1 | `GET /agent/me/thread` → `mc thread` | ✅ **the stage** — the genuine gap (F3) |
> | — | delete `get_last_checkpoint` + its `noqa: F401` shim | ✅ ride along; §8.1 closed, zero callers |
> | 5.2 | `mc recover --brief` | → Stage 2, same change as retiring the F7 double-inject |
> | 5.3 | MEMORY.md pointer | → Stage 2, it is an instruction, not a capability |
> | 5.4 | Hermes MCP tools | ⛔ **cut** — see F11; a tool with no trigger, plus a memory hazard |
> | 5.5 | JSONL rotation | ⏸ separate ticket — unrelated to context recovery, and see the note below |
>
> **Two passes shrank this stage from six items to one and a half**, and that is the finding,
> not an accident: the first draft proposed building four things MC either already had (F10)
> or could not safely use (F11).
>
> On 5.5: the precedent to copy, `scripts/rotate-gateway-logs.sh`, rotates logs for the
> **retired** OpenClaw Gateway (ADR-039) and is scheduled **nowhere** — the crontab line
> exists only as a comment. Lesson for whoever picks it up: an unscheduled rotation script is
> theatre. Ship the schedule or don't ship it.

### 5.1 `GET /agent/me/thread` → `mc thread`

New endpoint in `backend/app/routers/agent_scoped.py`. Mirrors the semantics of
`tasks.py:2632` so behaviour is consistent between operator and agent views.

| Param | Type | Meaning |
|---|---|---|
| `task_id` | uuid, optional | explicit override; omitted → resolved per §8.2 |
| `limit` | int, 1–200, default 50 | page size |
| `before_seq` | int, optional | backward pagination (older) |
| `since_seq` | int, optional | forward delta |

`since_seq` and `before_seq` are mutually exclusive → 400, mirroring `tasks.py:2659-2663`.

Response: ascending `seq` order, `has_more_before` flag, same shape as the user endpoint.
A task with no thread reads as an **empty page**, never 404, and **never creates** the
thread (matching `tasks.py` behaviour).

**Two hard constraints:**

1. 🔴 **Reading must NOT touch the ack cursor — and the trap is wider than it looks.**
   If reading acked, an agent rebuilding its context after a restart would **swallow its own
   undelivered mail** — and comm_v2 would lose the at-least-once guarantee F9 just
   confirmed intact.
   **Rule: `mc inbox` acks (consume). `mc thread` never acks (look up).**

   ⚠️ **Verified 2026-07-28, and this is the part that would have bitten:** the obvious
   helper to reuse, `_resolve_agent_threads_with_cursors` (`agents.py:2619`), is **not
   read-only**. It *creates* `AgentThreadCursor` rows on first sight and **fast-forwards them
   past all history** for tasks in `done`/`failed` (Befund C, live pilot 2026-07-20). A
   `mc thread` built on it would silently skip messages on a re-opened task.
   → `mc thread` must resolve the thread via `task.thread_id` directly and **must not create
   or advance any cursor**. The test asserts on *both* cursor columns **and** on
   cursor-row-count, so an accidental create fails too.
   ✅ Independently confirmed by the author of the comm_v2 read path — see F12, which also
   documents the `created_any` signal any *other* read path would have to carry. Resolving
   by `task.thread_id` avoids that whole machinery, which is precisely why it is the right
   shape here.

2. **Own tasks only.** The agent may read threads of tasks assigned to it. No fleet-wide
   reading. Least privilege.

### 5.2 ~~`GET /agent/me/resume` → `mc resume [task]`~~ — **cancelled, see F10**

**Do not build this.** `mc recover` (ADR-024) already occupies this slot: same read-only
discipline, same "where was I" job, already wired into SOUL and poll.sh. A second verb next
to it would be a second lifecycle for one concept — exactly what
`feedback_no_second_lifecycle` warns against, and agents would have to be taught which of
two near-identical commands to reach for.

What survives from this section is a **refinement of the existing verb**, and it is small:

- `mc recover --brief` — the ~600–800 char spine (status · checklist with
  `← HIER WEITERMACHEN` · workspace · last 5 thread lines · pointer to `mc thread`) instead
  of the full budget-capped dispatch prompt. `build_recovery_context`
  (`task_context_builder.py:883-951`) already produces the checklist-with-marker and the
  workspace hint, so this is composition, not new logic.
- Whether it becomes the *default* is a **Stage 2** question, because that is the same
  change as retiring the F7 double injection — and it should be made with the measurement
  in hand, not before.

**Open question §8.2 is thereby answered for `mc recover`: it takes no `task_id` and derives
from the task table** (`agents.py:3078-3085`). That was already the right call — see §8.2.

### 5.3 Make MEMORY.md visible (F6)

**Pointer in SOUL.md, not full injection.** Full injection would burn MEMORY.md into every
turn and the file grows. A pointer makes the memory *findable* instead of invisible, and
stays consistent with Decision B.

*(Open: whether the pointer belongs in the SOUL template or in the `mc resume` payload —
arguably both. See §8.)*

### 5.4 Bridge parity — **Hermes is cut from Stage 1** (F11)

- **Grok** — has shell `mc` (PR #150) → inherits `mc thread` automatically once the endpoint
  exists, and already has `mc recover`. Nothing to build.
- **Hermes — deliberately deferred.** The earlier plan (add `mc_recover` + `mc_thread` to
  `scripts/mc-mcp.py`) does not survive F11. Three reasons, each sufficient on its own:

  1. **Nothing would ever call them.** MC's SOUL does not reach Hermes, and the bridge sends
     nothing on restart. A tool with no trigger is dead weight — and it would *look* like
     parity while delivering none. The blast radius is at least contained: `mc-mcp.py` is
     registered only by `hermes-config-patch.py:28-41`, Grok has no MCP, and Docker agents
     use a different registry — so this is Hermes-only either way.
  2. **`mc_recover` would return the full dispatch prompt**, whose body re-instructs ACK,
     checklist creation and hand-off (`hermes-bridge.py:250-271`) → duplicate ACKs and
     duplicate checklists. It must not ship before `--brief` exists.
  3. **The auto-memory hazard (F11)** needs a bounded payload *and* the two unverified
     questions answered first (session auto-resume; whether background review runs
     unattended).

  **The real Hermes gap is a trigger, not a tool** — restart detection in the bridge that
  pastes a compact re-entry line. That is Stage 2 fleet parity (§6.3), where it belongs, and
  it should carry hard output caps modelled on the existing precedents (`mc_logs:484` caps
  at 4000 chars; `mc_task_detail:232` caps at 5 comments × 200 chars).

  Separately and independently of this design: **Hermes' MC instructions live in an
  untracked host file** (`~/.hermes/skills/mission-control/SKILL.md`). That is a drift risk
  worth its own ticket.

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

1. ~~SOUL instruction~~ — **already there** (`SOUL.md.j2:79`, `:2935`, ADR-024). Nothing to
   write; what is missing is evidence that it is *obeyed*, which is item 2. Revisit the
   wording only if the numbers say the instruction is being skipped.
2. ⭐ **Instrumentation — the point of this stage, and now cheaper than planned.** Record,
   per context-loss event, whether `mc recover` was called **before the first write**. The
   endpoint already exists and already sets a Redis key per recovery
   (`mc:recovery:attempt_id:<task_id>`, `agents.py:3094`), so the call is observable
   without touching the agent side at all. This answers the question the rejected gate was
   guessing at: *do agents actually forget?*
3. **Fleet parity (F4)** — Hermes/Grok get restart *detection* + a recover wake-up through
   the adapter contract (ADR-071 + TCK). Stage 1 gives Hermes the *ability*; this gives it
   the *trigger*. Needed regardless of any gate.
4. **Remove the double injection (F7)** — `recover_task` pastes the brief spine, not the
   full dispatch prompt; i.e. make `--brief` the default for the poll.sh path. Re-word the
   stale `CLAUDE.local.md` rule while touching it.

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

1. ✅ **CLOSED — Migration 0082.** See the resolved block under F8: consolidation onto
   `TaskChecklistItem` per ADR-020 §A4. `get_last_checkpoint` has zero callers → **delete**,
   do not revive.
2. ✅ **CLOSED — how does the read resolve "my task"?** Not "default vs. explicit" — the
   real answer is *which* default, and the codebase already settled it. Three patterns
   exist and they disagree:

   | Pattern | Used by | Shape |
   |---|---|---|
   | (a) task-table only | `/me/active-task-recovery` (`agents.py:3078`) | ignores `current_task_id`; `assigned + status IN (in_progress, blocked, review) ORDER BY updated_at DESC`; soft `{active: false}`, never 4xx |
   | (b) hybrid, pointer as *validated hint* | `/me/poll` (`agents.py:2816-2839`) | pointer first, re-checked against ownership **and** status, then falls back to (a) |
   | (c) bare pointer, 409 | `mc ask` (`:1363`), `mc msg` (`:1495`), help/delegate/clarification | no fallback, no status check |

   **Decision: `mc thread` follows (b), plus an optional explicit `task_id`.** The deciding
   fact is not crash-staleness but something sharper: **under `use_subagent_dispatch` every
   non-lead worker's `current_task_id` is null by design** — every set site except the
   heartbeat is gated on `not (use_subagent_dispatch and not agent.is_board_lead)`
   (`task_lifecycle.py:372`). A (c)-style endpoint would be **unusable for most of the
   fleet**, and a pointer-only default would serve board leads alone. `task_lifecycle.py:249`
   says it outright: *`current_task_id` can only track ONE task and is therefore just a
   hint, not a lock.*

   Corollaries worth carrying into the plan:
   - The pointer survives restarts uncleared, and the **only** convergent repair is the
     heartbeat self-heal (`agents.py:3419-3484`) — the watchdog does not reconcile it
     (zero `current_task_id` hits in `watchdog/`).
   - Do **not** reuse `_resolve_active_task_for_agent` (`agent_scoped.py:539`) unchanged:
     its fallback is a bare `session.get(Task, current_task_id)` with **no status and no
     ownership re-check**, so it hands back a stale pointer at a `done` task as-is.
   - No 422 on "nothing found" — return an empty page. A recovery-shaped read must not
     explode when there is nothing to recover.
3. **MEMORY.md pointer placement** — SOUL template, `mc recover --brief` payload, or both.
4. **Host-form recovery** — Boss-Host has its **own copy** of `poll.sh`
   (`docker/boss-host/poll.sh`, 436 lines) rather than sharing `docker/shared/poll.sh`.
   Stage 2 parity work should decide whether to converge them or keep them separate. Drift
   between the two is a standing risk.
5. **Should `mc thread` be readable for *completed* tasks?** Useful for "how did I solve this
   last time"; widens the authorization surface.

---

## 9. Re-entry for the next session

Trigger phrase: **"Kontext-Recovery weiter"**.

### 9.1 State in one paragraph

Direction unchanged and approved (§3: **A — reconstruct over preserve**, **B — pull over
push**). The hard gate stays deferred (§4). Two passes of verification shrank Stage 1 from
six items to **one and a half**, because most of what the first draft proposed either already
existed (F10, `mc recover`) or could not be used safely (F11, Hermes). Both open questions
are **closed** (§8.1, §8.2). Collision check against the parallel comm_v2 work is **done and
clean** (F12). **Nothing has been implemented — this branch is documentation only.**

### 9.2 What to build, in order

| # | Item | Why this order |
|---|---|---|
| 1 | **Instrumentation**: per context-loss event, did `mc recover` happen before the first write? | Smallest item, but the only one needing **wall-clock time** to yield data. Start it first so it accumulates while #2 is built. Nearly free: the endpoint already writes a Redis key per recovery (`agents.py:3094`). |
| 2 | **`mc thread`** — `GET /agent/me/thread` in `agent_scoped.py` + CLI verb + tests (§5.1, §5.6) | The one real capability gap (F3). Purely additive, no risk. |
| 3 | delete `get_last_checkpoint` (`task_context_builder.py:809`) + its `noqa: F401` re-export (`dispatch.py:587`) | Rides along; zero callers, §8.1 closed. |
| — | Hermes, the gate, the valve, JSONL rotation | **Not now.** #1 decides the first three; rotation is an unrelated ticket. |

Honest limit of the measurement, stated so nobody over-reads it: the backend only sees a
context loss **where poll.sh reports one**, so it is blind for Hermes and Grok — the same
blindness that helped kill the gate. That is acceptable here, because for those two the
answer is already known (no recovery at all, F4). We measure where we don't know.

### 9.3 The five things that will bite

1. 🔴 **`mc thread` must never touch the ack cursor.** `mc inbox` acks (consume), `mc thread`
   never does (look up). Otherwise an agent rebuilding context eats its own unread mail and
   comm_v2 loses at-least-once. **The test comes before the endpoint.**
2. 🔴 **Do not reuse `_resolve_agent_threads_with_cursors`** — it creates cursors *and*
   fast-forwards them (§5.1, F12). Resolve `task → task.thread_id` instead. That is not a
   style choice; it is what makes #1 enforceable.
3. **Do not modify `agents.py`.** Agreed boundary with the Telegram team-chat work (F12).
   Costs nothing — the new endpoint belongs in `agent_scoped.py`.
4. **Task resolution follows `/me/poll`** (`agents.py:2816-2839`): pointer as *validated*
   hint, task-table fallback, optional explicit `task_id`. Not `current_task_id` alone — it
   is null by design for every non-lead worker under subagent dispatch (§8.2).
5. **Empty page, never 422.** A recovery-shaped read must not explode when there is nothing
   to recover.

### 9.4 Before writing code

1. Read this document top to bottom — especially §4 (why no gate) and F10/F11/F12.
2. Then `docs/decisions/024-*` (`mc recover`, read-only recovery) and
   `docs/decisions/071-w21-delivery-foundation.md` (adapter contract).
3. Templates to copy rather than invent: `backend/tests/test_inbox_pull.py` (agent-token
   fixtures + cursor assertions) and `backend/tests/test_thread_read_api.py` (pagination
   cases). The new file combines them.
4. `writing-plans` on §9.2, then TDD per `superpowers:test-driven-development`.

### 9.5 Loose ends deliberately left

Each is real, none belongs in this stage:

- **Hermes' MC instructions live in an untracked host file**
  (`~/.hermes/skills/mission-control/SKILL.md`) — referenced once in the repo, generated
  nowhere. Own ticket.
- **Two Hermes unknowns** that would change F11: does `hermes --yolo` auto-resume from
  `~/.hermes/state.db` (287 MB)? Is the background review suppressed unattended?
- **ADR-020 rollout never finished**: `POST /checkpoint` should have been deleted after
  2 releases (still a 410 shim), `task_checkpoints` dropped after 3 weeks (still there,
  still read by a live GET).
- **Should `mc thread` read the agent's DM thread too?** DMs entered message scope with the
  Telegram work (F12). Argument exists; v1 says task threads only.
- **`scripts/rotate-gateway-logs.sh` is scheduled nowhere** and serves a retired component.
  Cautionary precedent for the JSONL rotation: ship the schedule or don't ship it.

### 9.6 Working state

Branch `feat/context-reconstruction`, worktree of the same name, **pushed** (backup) and
based on `origin/main` `5a63c678`. Documentation only — no code, no migrations, nothing to
revert. The original base predated comm_v2 entirely (see §0); **verify the base is current
before trusting any `file:line` here.**

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

Second pass the same day, before planning: the branch was rebased off a stale pre-comm_v2
base and every `file:line` re-verified; §8.1 and §8.2 were researched and closed; and F10
was found — `mc recover` already does most of what Stage 1 proposed to build, which cut the
stage roughly in half and turned one item (Hermes) from an enhancement into a live defect.
Lesson worth keeping: the first pass searched for the *mechanism* it expected
(`--continue`, `--resume`) and concluded it was absent; MC's answer was a CLI verb under a
different name. **Search for the capability, not the implementation you have in mind.**
