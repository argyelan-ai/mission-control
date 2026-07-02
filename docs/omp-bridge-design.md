# omp-bridge — MC Agent Runtime Design

**Status:** PROTOTYPE / design draft (Phase 0/1, non-destructive). Not wired into the backend yet.
**Author:** lead-architect synthesis of 4 research spikes (omp event schema · MC harness gap · runtime architecture · omp capabilities), revised after adversarial judge-panel review.
**Scope of THIS document:** the design + the prototype files under `docker/omp-bridge/`. Backend wiring is a gated Phase-2 step (see §7).
**Purpose in one line:** replace the tmux screen-scrape turn-detector with omp's structured event stream so the harness *deterministically knows* when a turn ended and, if the task is not on review, sets a **human-visible, reversible blocker** instead of leaving it silently `in_progress` forever — backed by a runtime-agnostic backend watchdog so the guarantee does **not** depend on any single harness process being flawless.

---

## 1. Problem recap — the silent-abort gap (cited)

Today an MC agent that runs `openclaude` inside a container (`agent_runtime == "cli-bridge"`, `mc-agent-base:latest` image) is driven by a **poll + screen-scrape** loop:

- `poll.sh` (tmux Window 1) pulls task state from `GET /api/v1/agent/me/poll` every 5 s and pastes the prompt into the interactive `openclaude` pane (Window 0). It **never waits for completion** — "Kein Warten auf Completion — claude meldet sich selbst via MC API" (`docker/mc-agent-base/poll.sh:465`). Completion is *only* ever signalled when the agent itself calls the `mc` CLI (`mc ack` → in_progress, `mc finish`/`--review` → done/review, `mc blocked` → blocked).
- Turn liveness is inferred by **regex over `tmux capture-pane`** (`docker/mc-agent-base/lib/turn-state.sh`): `working | crashed | idle | unknown`.

**The confirmed failure mode** (turn-state.sh header, lines 6-11): `openclaude` treats a transient API error (`fetch failed` / `Connection error` / `5xx`) as a *turn-abort* and drops back to the interactive `❯` prompt. **The process stays alive, nothing PATCHes a terminal status, and the task stays `in_progress` forever.**

The turn-state helper was built to close this loop but has two concrete escape hatches (both confirmed in the harness-gap research):

1. **`unknown` is a permanent dead-zone.** The idle marker is the *anchored* `^❯ *$` only. If the prompt line has residual text / a partial redraw / a non-`❯` glyph, or the API-error text scrolled past the 50-line capture window before 3 consecutive `crashed` reads accumulate, `detect_turn_state` returns `unknown`. `poll.sh`'s `unknown` handler only resets `CRASHED_COUNT=0` — it accrues no stagnation and never calls `report_blocker`.
2. **Stagnation monitoring silently disarms.** The stagnation path is gated on `poll.sh`'s local `CURRENT_TASK_ID` (poll.sh:705). A transient backend `idle` response nulls it (poll.sh:849-857); subsequent `working` responses only `touch` the recycler marker and never re-populate `CURRENT_TASK_ID` — so monitoring is off while the DB task is still `in_progress`.

The backend even *records the tell* but never acts on it: the heartbeat handler self-heals `agent.current_task_id` from the DB and, when `poll.sh` reports `idle` against an active task, sets `agent.status = idle` while leaving `current_task_id` pointed at the task — verified at `backend/app/routers/agents.py:2445-2467` (comment: *"Task assigned aber Agent nicht aktiv, was ist los?"*). Nothing converts that signal into a blocker. The only remaining catch is `_check_stale_in_progress` in `task_runner.py:811-970`, which waits **15–60 min** (role-based `_idle_threshold_for`), emits a `task.stuck` event / tiered recovery, and **never sets `status=blocked`.**

**Net:** for 15–60+ minutes (indefinitely in blocker terms) a silently-aborted task shows `in_progress` / `working` with no blocker and no human signal.

**Design lesson baked in below:** the previous in-container detector (`turn-state.sh`) failed precisely because it was a *single unreliable process* that was trusted to always fire. This design must not simply move that single point of failure into a new process (`bridge.py`). See §3.5 — the authoritative net is a backend DB-only watchdog; the stream parser is a fast-path on top of it.

---

## 2. Why omp's event stream *should* close it — and the one claim we have NOT yet verified

`omp` (v16.2.13) in `-p --mode json` emits a **self-terminating NDJSON lifecycle stream** on stdout. The decisive property: **the outcome of a run is legible from the stream regardless of exit code** (all normal outcomes exit 0). The bridge reads the stream, not the exit code.

Key events and fields the bridge keys on (raw sample: `docker/omp-bridge/rpc/json-stream.ndjson`):

| Event | Decisive field(s) | What it tells the bridge |
|---|---|---|
| `session` | first line, `id`, `cwd` | run started, identity for logging |
| `turn_end` | `message.stopReason`, `message.content[]`, `toolResults[].isError` | one LLM round ended; the **final** `turn_end` is the completion oracle |
| `agent_end` | terminal, exactly once; `messages[-1].stopReason` | the agent loop ended cleanly. **Presence = normal termination.** |
| `tool_execution_end` | `isError`, `details.exitCode` | tool-level pass/fail (bash exit 1 → `isError:true`) |
| `message_end` (assistant) | `stopReason`, `errorMessage`, `errorId` | model/provider error surfaces here as `stopReason=="error"` |

**The completion contract** (from the research, grounded in captured streams):

- **Task finished** = `agent_end` present AND final `stopReason == "stop"` AND the sentinel+reflection contract of §3.4 is satisfied.
- **Stopped-without-finishing** falls into an **abort class** the bridge must treat uniformly (§3.3), because — as the judge panel correctly flagged — a transient failure can take more than one shape and we have **not** empirically pinned which one omp+Claude produces:
  1. `agent_end` present but final `stopReason ∈ {"toolUse","error"}`.
  2. `agent_end` **absent** at process exit (crash / SIGKILL).
  3. **Neither `agent_end` nor process exit** — a wedged/hung omp (deadlocked provider read, internal infinite retry). The stream simply stops advancing. This is the *same failure family* as the original openclaude bug and is caught only by the bridge's own wall-clock watchdog (§3.3), not by any stream field.

### 2.1 ⚠️ The single most load-bearing claim is UNVERIFIED (verify-before-assert)

The original openclaude failure is a **transient mid-run** `fetch failed` / `Connection error` / `5xx` against a *live* Claude endpoint. The earlier draft asserted as fact that this surfaces as a first-class `stopReason == "error"`. **That is not supported by the evidence on disk and must not be stated as fact:**

- The only `stopReason:"error"` sample is `docker/omp-bridge/rpc/err2-stream.ndjson`, and it is a **deterministic config/preflight error** (Azure `gpt-5.2`, `errorMessage:"…base URL is required…"`, `duration:1.47ms`, `totalTokens:0` — it never contacted a model), and **not even the Claude provider**. Verified by inspection.
- `err-stream.ndjson` (despite the name) is a **successful** `say hi` run — 52× `stopReason:"stop"`, no error. Verified.
- **No captured stream exercises a transient mid-run network abort, and none exercises Claude.** So we have **zero ground truth** on how omp+Claude renders a transient 5xx: it *could* surface as `stopReason:"error"`, or omp could internally retry, or crash (no `agent_end`), or hang.

**Consequence for the design:** the bridge must **not** assume `stopReason:"error"` is the only shape. It classifies the whole **abort class** `{final stopReason=="error"}` OR `{final stopReason=="toolUse"}` OR `{no agent_end at process exit}` OR `{bridge wall-clock timeout with no progress}` and routes all of them through the same bounded-retry-then-blocked path (§3.3). Capturing a **real** transient-error stream (run omp against live Claude, blackhole the endpoint mid-run, record the NDJSON) is a **hard Phase-2 gate** (§7, gate #8) — the gap is not claimed closed until that stream exists.

Two negative findings that also shape the design:
- **`rpc`/`rpc-ui` modes are unusable headless** — under `-p` with closed stdin they emit only a handshake and ignore the prompt. **`json` is the only correct headless mode.**
- **`--advisor` injected nothing observable** into the `-p json` stream on a clean task; it is *not* a reliable blocker channel. **We do not need it** — the json stream already gives MC everything to drive ack/finish/blocker externally.

Crucially, `stopReason=="stop"` means only *the model ended its turn*; it does **not** prove the user's goal was met. So the bridge's rule is: **stream says the turn ended → the bridge decides ack/finish/blocker by combining the stream signal with the task's own review/terminal state**, never by trusting `stop` alone.

---

## 3. Architecture — the omp-bridge runtime

### 3.1 Shape (what changes, what stays) — corrected process topology

`omp-bridge` is a **third harness image** parallel to `mc-agent-base` (openclaude) and `mc-claude-agent` (native claude). It **reuses the entire cli-bridge container lifecycle** and differs only in the Window-0 driver.

**Topology decision (resolves the earlier "where does omp run?" contradiction):** `bridge.py` is the **single persistent Window-0 process**. omp is a **short-lived subprocess of bridge.py**, not a pane of its own. This is the only topology that keeps the health-check, the Sessions live-terminal, and the recycler all working:

```
Container (agent_runtime == "cli-bridge", image = omp-bridge:latest)
├─ Window 0: bridge.py  (PERSISTENT idle loop — the long-lived process)
│            • pulls tasks from GET /api/v1/agent/me/poll   (UNCHANGED backend contract)
│            • prints a stable `OMP_BRIDGE_READY` line to ITS OWN pane once polling → health-check anchor
│            • per task: spawns `omp -p --mode json …` as a SUBPROCESS, captures its stdout
│            • TEES omp's NDJSON to the pane (so the Sessions tab shows live output) while parsing it
│            • runs an INDEPENDENT wall-clock watchdog over the subprocess (§3.3)
│            • maps the run outcome → mc CLI (ack / finish / review / blocked / comment)
│            • is the process the recycler pgreps and the health-check waits on
├─ Window 1: (unused / reserved) — the poll+scrape split of the old design is collapsed into Window 0
└─ Window 2: omp-recycler.sh  (FORKED — NOT byte-identical; see §3.1.1)
```

Why bridge.py must be the pane process, not omp:
- **Health check** `wait_for_agent_healthy` / `_wait_for_window_ready` polls `{session}:0` (`docker_agent_sync.py:594/603`). A one-shot omp that exits between tasks leaves the pane empty → false timeout → spurious `HEALTH_TIMEOUT_RECREATE` rollback. A persistent bridge.py printing `OMP_BRIDGE_READY` gives a stable, non-ambiguous anchor (do **not** rely on the loose `'$ '` shell-prompt match).
- **Sessions live-terminal** attaches to the pane; teeing omp's NDJSON there keeps that tab populated.
- **Recycler** pgreps the pane process; a persistent bridge.py is a stable target, a one-shot omp is not.

**Unchanged contracts:** the `/me/poll` pull, the `mc` CLI push (copied byte-identical into the image), the bootstrap-token flow (`GET /api/v1/internal/bootstrap`), the 3-window tmux layout. **Removed:** `tmux load-buffer`/`paste-buffer`/bracketed-paste/CR-vs-LF prompt injection and `turn-state.sh` screen-scraping. The omp harness **owns turn-state via structured events + a wall-clock watchdog** — the scrape stagnation path is dropped in this image to avoid double-reporting (see §6).

#### 3.1.1 The recycler is FORKED, not unchanged

The judge panel correctly flagged that `recycler.sh` **cannot** be copied byte-identical. Verified in `docker/mc-agent-base/recycler.sh`: it `pgrep`s a persistent Window-0 *process* (`PROCESS_NAME`, matching `comm=claude/openclaude`) and `respawn-pane -t {session}:0 -k`s it whenever that process is dead/idle, and its stale-lock guard greps for `poll.sh`. Under omp's one-shot model **none of those assumptions hold**: there is legitimately no persistent omp process, so a byte-identical recycler would read every between-tasks gap as a crash and respawn, and could drop the lock / idle-kill a busy run.

**Fork = `docker/omp-bridge/omp-recycler.sh`** with two changes:
1. **`PROCESS_NAME` points at the long-lived driver — `bridge.py`** (the persistent Window-0 process), never at the one-shot `omp` subprocess. `proc_alive` therefore tracks the real supervisor.
2. **Liveness/stale-lock gate = "is `bridge.py` alive AND is `TASK_LOCK_FILE` present?"** An absent one-shot `omp` subprocess is **never** treated as a crash. The stale-lock `pgrep` targets `bridge.py`, not `poll.sh` (which does not exist in this image).

Idle/RSS recycling still applies to the *bridge.py* driver via the existing marker/`RECYCLER_IDLE_ENABLED` mechanism; the semantics simply track the correct process.

### 3.2 How omp runs headless (the invocation)

Per task, `bridge.py` runs omp as a subprocess (grounded in the research's best-sample invocation):

```
omp -p \
  --cwd <MC-worktree-path> \        # never ~ (avoids omp's home→temp auto-relocate)
  --mode json \                      # ONLY correct headless mode
  --model claude-opus-4-8 \          # MC runs Claude, not Qwen (see §5)
  --approval-mode yolo \             # -p cannot prompt; unattended tool approval
  --no-session \                     # ephemeral; MC owns task identity (or --session-dir per task)
  --hide-thinking \                  # thinking is not task output
  --max-time <cap-seconds> \         # omp's OWN in-process timer (NOT the outer bound — see §3.3)
  "<task-prompt + reflection+sentinel contract (§3.4)>"
```

Notes:
- `--cwd <worktree>` is mandatory — a real worktree path is never home, so `--allow-home` is unneeded and omp will not silently relocate.
- Keep omp's own `task.isolation.mode = none` (current default) so omp does **not** create nested worktrees inside MC's per-task worktree.
- Because omp is `-p` one-shot and idempotent (each run is a fresh, stateless invocation with `--no-session`), it is **safe to re-run N times** for the same task — this is what makes bounded bridge-level retry (§3.3) correct.

### 3.3 Run outcome → MC lifecycle mapping (the core table)

`bridge.py` is a streaming NDJSON reducer wrapped in an **independent wall-clock supervisor**. It **drops `message_update`** (509/578 lines in the sample — pure token deltas) and reacts to top-level lifecycle events. State it tracks per run: `saw_agent_end`, `final_stop_reason`, `any_unresolved_tool_error`, `saw_sentinel`, `reflection_block`, plus supervisor state `last_stream_progress_ts`.

**Wall-clock watchdog (mandatory, closes the hang case).** `--max-time` is omp's *own in-process* timer; if omp's event loop is wedged (deadlocked provider read, TLS stall, internal infinite retry) there is **no guarantee it fires**, and bridge.py would otherwise block on `readline` forever — the silent hang, relocated. So bridge.py runs omp under its **own** timer:
- Hard cap = `--max-time + margin` (e.g. `+120 s`) as an absolute wall-clock deadline, AND
- a **no-progress** deadline: if no new NDJSON line arrives for `STREAM_IDLE_TIMEOUT` (e.g. 90 s), the run is declared hung.
- On either deadline the supervisor **`SIGKILL`s the omp subprocess** and classifies the run as `crash/hang` — from the outside, regardless of stream state.

**Retry-then-blocked policy (fixes the `mc failed` mis-mapping).** Verified against the MC lifecycle: `VALID_TRANSITIONS[FAILED] == {INBOX}` only (`backend/app/task_status.py:28`) and there is **no** backend auto-redispatch of a board-status `failed` task; `failed` also auto-unassigns the agent. So routing a 2-second network blip to `mc failed` produces a **dead, unassigned task Mark must manually re-open** — strictly worse than the hang it replaces. Therefore the abort class is handled by **bounded retry, then `blocked`** (`VALID_TRANSITIONS[BLOCKED] == {INBOX, IN_PROGRESS, FAILED}` — human- and recovery-friendly, verified `task_status.py:27`). `mc failed` is reserved for genuinely non-retryable outcomes only.

| omp run signal | Bridge decision | MC lifecycle action (`mc` CLI → backend PATCH) |
|---|---|---|
| `session` (first line) | run launched → claim work | `mc ack` → PATCH status `inbox→in_progress`, stamp `ack_at` |
| `turn_start` | new round begins | write/refresh `TASK_LOCK_FILE`; bump `last_stream_progress_ts`; optional `mc comment` heartbeat |
| `tool_execution_start` | tool running | keep-alive; report `working` to `/me/poll` heartbeat; bump progress ts |
| `tool_execution_end` `isError:false` | tool ok | progress only |
| `tool_execution_end` `isError:true` + `details.exitCode` | mark `any_unresolved_tool_error` | deferred to run end |
| **`agent_end` present AND final `stopReason=="stop"` AND sentinel present (per §3.4 anti-echo rule) AND valid reflection block extracted AND no unresolved tool error** | **genuine finish** | on a `require_review_before_done` board → `mc finish --review` (→ **review**); else `mc finish` (→ done). The 4-field reflection is the block bridge.py extracted from the final message (§3.4). |
| **`agent_end` present AND final `stopReason=="stop"` but sentinel MISSING / reflection missing or invalid / trailing unresolved tool error** | **semantic silent-abort or malformed completion** (§3.4) | `mc blocked --blocker-type technical_problem --question "omp-Turn endete (stopReason=stop) ohne gültige TASK_COMPLETE-Sentinel/Reflexion — Aufgabe evtl. nicht abgeschlossen oder Reflexion fehlerhaft; bitte prüfen/fortsetzen."` |
| **Abort class** — `final stopReason=="error"` (+ `errorMessage`) **OR** `final stopReason=="toolUse"` / `[Command cancelled]` (--max-time) **OR** `agent_end` absent at exit (crash) **OR** watchdog wall-clock/no-progress kill (hang) | **retryable transient/interrupt** → **bounded retry** | re-run `omp -p` up to `OMP_MAX_RETRIES` (e.g. 2) with short backoff, since the run is one-shot + idempotent (§3.2). If a retry finishes → normal finish path. If still in the abort class after all retries → `mc blocked --blocker-type technical_problem --question "omp brach nach <N> Versuchen ab (<class>: <detail>) — Aufgabe unvollständig, bitte prüfen/fortsetzen."` **Never `mc failed`.** |
| exit code `1` (pre-flight: model not resolvable / no credential) or `2` (CLI arg parse) — **no json session emitted at all** | **launch/preflight failure — genuinely non-retryable** (config/credential is deterministic, retrying repeats it — cf. err2-stream) | `mc blocked --blocker-type technical_problem --question "omp Launch/Preflight-Fehler (exit <code>): <stderr> — Konfiguration/Credential prüfen."` (also human-fixable; `failed` would just strand it) |
| agent itself emitted a blocker via its work | **agent-set blocker** — respect it | idempotency guard (§6): bridge does not override an already-terminal task |

> Note on `mc failed`: with the above, the prototype **does not route any transient/interrupt case to `failed`.** `failed` is left available only for a future, explicitly-classified non-retryable outcome (none currently identified in the omp stream) — and even then only because a human can re-open it to `inbox`. The earlier "→ backend redispatch applies" claim is **removed**: no such auto-redispatch exists.

**Determinism rule (the fix):** the bridge classifies **every** run into exactly one of {finish/review, blocked} the instant the run resolves — where "resolves" now includes the watchdog firing on a hang. There is **no path** where a turn ends (or hangs) and the task is left `in_progress` by the bridge. And because the bridge itself can die, the backend watchdog (§3.5) is the authoritative net behind it.

### 3.4 The completion contract — reflection + sentinel come from the prompt

The judge panel correctly flagged that the happy path was previously unimplementable: `mc finish`/`--review` **hard-requires** a 4-field reflection with the exact German headers and ≥80 chars, validated **locally before any HTTP call** (`docker/mc-agent-base/mc-cli/mc_cli/commands.py:326-363`, `_validate_reflection`; fields `## Was wurde gemacht`, `## Was hat funktioniert`, `## Was war unklar`, `## Lesson fuer Agent-Memory`). bridge.py is a dumb NDJSON reducer — it has no genuine reflection of its own. If it sent a stub, `mc finish` raises `UsageError`, **no status PATCH happens, and the task stays `in_progress`** — reopening the exact gap, now on the success path.

**Resolution — make reflection + sentinel part of the prompt-wrapping contract.** The task prompt is wrapped so the omp agent must end its **final assistant message** with, in this exact order:

```
## Was wurde gemacht
<…>
## Was hat funktioniert
<…>
## Was war unklar
<…>
## Lesson fuer Agent-Memory
<…>
TASK_COMPLETE
```

— i.e. the literal 4-header reflection block (exact headers) immediately followed by the `TASK_COMPLETE` sentinel as the **last non-empty line, alone on its line**, emitted **only when the goal is truly met**.

**What bridge.py scans (precise, anti-echo, anti-thinking):**
- **Only** the concatenated `content[]` entries with `type == "text"` of the **final assistant message** (the message carrying the terminal `agent_end` / final `turn_end`). **Never** `type == "thinking"`, **never** any `message_update` delta, **never** an earlier message. With `--hide-thinking` the model's conclusion must land in that final text; if it does not (e.g. truncated by max-time), the sentinel is absent → blocked, which is the safe direction.
- **Sentinel rule (anti-echo):** `TASK_COMPLETE` counts only if it is the **last non-empty line and is alone on that line**. This defeats (a) the model echoing the instruction mid-text and (b) a give-up line that merely *contains* the token (`"I'll continue later — TASK_COMPLETE"` fails the "alone on its own last line" test only if the model is disciplined — so we additionally require the preceding four headers to be present and non-empty; a give-up message that skips the reflection block fails the reflection extraction).
- **Reflection extraction:** bridge.py slices the block from the first `## Was wurde gemacht` header to the line before the sentinel, verbatim, and passes it to `mc finish`. Before calling, bridge.py runs the **same** `_validate_reflection` logic locally (4 headers present + ≥80 chars). If extraction or validation fails → **`mc blocked`** (the "malformed completion" row of §3.3), **never** a silent drop to `in_progress` and **never** a boilerplate stub that would poison Rex's review learning-loop.

**Completion gate (pseudocode):**
```
reflection_block = extract_reflection(final_assistant_text)     # None if headers absent
sentinel_ok      = last_nonempty_line(final_assistant_text) == "TASK_COMPLETE"
finished = (saw_agent_end and final_stop_reason == "stop"
            and sentinel_ok
            and reflection_block is not None
            and validate_reflection(reflection_block)  # 4 headers + >=80 chars
            and not any_unresolved_tool_error)
if finished:      mc_finish(reflection_block, review=board_requires_review)
else:             classify_and_block_or_retry()   # §3.3 — never leaves in_progress
```

**Replay tests (shipped with the prototype, run against captured NDJSON):**
- **finish fixture:** a synthetic captured stream whose final text carries the 4-header block + trailing `TASK_COMPLETE` → assert `extract_reflection` yields a block that **passes** `_validate_reflection` and that the gate returns `finished`.
- **malformed-reflection fixture:** final message with sentinel but a missing header / <80 chars → assert the gate returns `blocked`, not finish, and never `in_progress`.
- **anti-echo fixture:** `TASK_COMPLETE` appearing mid-text (echo) and a give-up line containing the token, both without the header block → assert **not** finished.
- **crash fixture (synthetic):** a captured stream **truncated before `agent_end`** → assert abort-class → retry-then-blocked.
- **hang fixture (synthetic):** a stream that stops advancing with no terminal line → assert the watchdog fires and classifies `crash/hang`.

The **live** Claude sentinel behaviour (false-positive / false-negative counts) is **not** settled by these Qwen-based fixtures and is a hard Phase-2 gate (§7 gate #8).

### 3.5 The authoritative net — backend DB-only watchdog is the PRIMARY fix (promoted from Phase-2 optional)

The judge panel's most important structural point: **bridge.py is now the sole component that both reads the stream and PATCHes terminal status.** If bridge.py throws in the reducer, blocks on `readline`, or dies **after `mc ack` (inbox→in_progress, `ack_at` stamped) but before the terminal PATCH**, the task is stuck `in_progress` with no blocker — byte-identical to today's gap. The stream watchdog of §3.3 protects against a hung *omp*, but nothing in the container protects against a dead *bridge.py*. That is the same "trust one process to always fire" assumption that sank `turn-state.sh`.

Therefore the **runtime-agnostic backend DB-only watchdog is the PRIMARY, mandatory layer**, not a "recommended Phase-2 nicety":

> **Signal:** `task.status == in_progress` AND `ack_at IS NOT NULL` AND the agent's heartbeat status has been `idle` for N consecutive beats AND no terminal PATCH since `ack_at`.
> **Action:** auto-PATCH `blocked` (with `blocker_type = technical_problem`) after a ~90–120 s grace (vs today's 15–60 min), emitting an activity event.

Properties that make it the authoritative net:
- It **survives a dead bridge.py** (and a dead poll.sh, and a dead host worker) — it reads only DB + heartbeat state.
- It is **runtime-agnostic** — it also closes the *original* openclaude gap and protects host agents, so it is a net reliability win independent of omp.
- `IN_PROGRESS → BLOCKED` is a valid transition (`task_status.py:24`), and `blocked` is reversible (`→ {inbox, in_progress, failed}`), so a false-positive is a cheap human clear, never a dead task.

**bridge.py's stream parsing is the fast-path optimisation on top of this net** — it reports the *right* terminal state in seconds and with a precise reason, so the backend watchdog almost never has to fire. But the *guarantee* that no task hangs comes from the backend layer, which does not depend on any harness process being flawless.

This watchdog is listed as **Phase-2 item #0 (mandatory, first)** in §7 — it is a small backend addition, separate-PR-able, and it is the part of this design that actually closes the gap end-to-end.

---

## 4. How it slots in alongside openclaude

**Recommended seam (minimal-invasive, "nothing ripped out"):** keep `agent.agent_runtime == "cli-bridge"` and introduce the new harness through the **runtime registry (axis B)** — exactly the pattern by which openclaude and native-claude already coexist.

MC has two orthogonal runtime axes:
- **Axis A — `agent.agent_runtime`** (`cli-bridge | claude-code | manual | host`): the execution substrate. `dispatch_delivery._deliver()` branches on it (`backend/app/services/dispatch_delivery.py:81/102/130`, unknown → `unsupported_runtime` at `:153`).
- **Axis B — `agent.runtime_id` → `runtimes` table**: selects the docker image + `.env` token routing.

Because **openclaude vs claude is already an axis-B choice inside a single `cli-bridge` literal**, omp-bridge slots in the same way:

1. **Image:** new `docker/omp-bridge/` → `omp-bridge:latest`. Add a `pick_image_for_runtime` branch keyed on the **first-class discriminator** `runtime.runtime_type == "omp"` (see §5) → `OMP_IMAGE`, alongside CLAUDE/OPENCLAUDE. Add an `x-omp-agent-base` compose anchor mirroring `x-openclaude-agent-base`.
2. **No axis-A change:** `agent_runtime` stays `cli-bridge`, so `dispatch_delivery` already routes it (no new dispatch branch, no `unsupported_runtime` risk), `restart_docker_agent_container`'s 3 modes already work, and `switch_agent_runtime` already admits it.
3. **Selection = the existing switch flow.** `switch_agent_runtime` (`agent_runtime_switch.py`) already does DB-rebind → `write_compose_agents()` → `detect_image_change` → `force_recreate` onto the new image → `wait_for_agent_healthy` → `agent.runtime_switched`, with full `_rollback` on failure. omp-bridge is a pure docker/cli-bridge switch — the simpler of the two existing flows.

**Non-destructive & opt-in per agent:** switching *one* agent to the omp runtime row flips only that agent's image on its next recreate. Every other agent is untouched. Rolling back = switch the runtime row back → `force_recreate` onto the previous image.

**Alternative (a distinct `agent_runtime == "omp-bridge"` literal)** ripples through `dispatch_delivery.py:145`, `_ensure_agent_switchable`, and the runtime-literal enums in `models/file_index.py` + `models/model_usage.py`. **Rejected** — the registry seam honours "selectable per agent" without any of that.

---

## 5. Claude-model wiring + auth — a first-class `omp` discriminator is MANDATORY (slug-collision fix)

**MC agents run Claude (Opus), not Qwen.** omp treats Anthropic/Claude as a first-class provider in 16.2.13 and the Claude model catalog is baked into the binary (verified via `strings`).

### 5.1 The slug collision (why the earlier "optional" wiring was wrong)

The earlier draft used the runtime **slug** both to select the omp image (`omp-*` prefix) and to carry Claude auth (`anthropic-claude-*` prefix so the OAuth token flows). **These are mutually exclusive on one slug field**, and **four** consumers all hardcode the *same* discriminator `slug.startswith("anthropic-claude-")` — verified:

- `compose_renderer.pick_image_for_runtime` → returns `CLAUDE_IMAGE = mc-claude-agent:latest` for `anthropic-claude-*` (`compose_renderer.py:88`).
- `internal.build_runtime_env` → emits `CLAUDE_CODE_OAUTH_TOKEN` (and **no** `OPENAI_*`) for `anthropic-claude-*`, else emits `OPENAI_BASE_URL`/`OPENAI_MODEL` (`internal.py:55-63`).
- `docker_agent_sync.py:212/216/311` → `is_anthropic` gating of the env-shim (token vs `OPENAI_*`).

So:
- If the omp slug is `anthropic-claude-*` → `pick_image_for_runtime` returns the **Claude image**, and the omp image is **never selected**.
- If the omp slug is `omp-*` → `build_runtime_env` emits **no Claude token** *and* writes an `OPENAI_*` openclaude shim with no Claude auth.

Either way it breaks. The earlier "No new backend branch strictly required for Claude auth" and "build_runtime_env optional" claims were therefore **wrong**.

### 5.2 Resolution — branch on `runtime_type == "omp"`, not on the slug prefix

Introduce a first-class discriminator that is **not** the slug prefix — the runtime row's `runtime_type == "omp"` — and branch on it consistently in **all three** places (**all mandatory**):

1. **`compose_renderer.pick_image_for_runtime`** — add `if runtime.runtime_type == "omp": return OMP_IMAGE` **before** the `anthropic-claude-` slug check, so an omp runtime never mis-selects the Claude image.
2. **`internal.build_runtime_env`** — treat `runtime_type == "omp"` like the anthropic path: **emit `CLAUDE_CODE_OAUTH_TOKEN`** from the Vault (`claude_code_oauth_token`) **and skip** the `OPENAI_*` shim. The omp entrypoint then re-exports it as omp expects (omp's precedence is `ANTHROPIC_OAUTH_TOKEN` env first): `export ANTHROPIC_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN"`.
3. **`docker_agent_sync.py`** — make `is_anthropic` / `is_anthropic_slug` treat `runtime_type == "omp"` as **token-bearing** (write the OAuth token, suppress `OPENAI_*` env). This is a **required** change — it was omitted from the earlier §7 (which only mentioned readiness/timeouts).

- **Model selection:** the omp runtime row supplies `--model claude-opus-4-8` (via `model_identifier`), mirroring how a vllm/cloud row supplies `OPENAI_MODEL`.
- **Auth token source:** MC's existing Vault `claude_code_oauth_token`, routed by the `runtime_type == "omp"` branch above and mapped onto omp's `ANTHROPIC_OAUTH_TOKEN`. Fallbacks omp supports but the prototype does not need: `ANTHROPIC_API_KEY`, the `auth-broker` vault, Foundry/mTLS.

**Verification honesty:** omp's Claude path was *not* run end-to-end in the spike (no credential injected, per non-destructive scope). The first live test (Phase-2 gate, §7 #8) must confirm `omp -p --model claude-opus-4-8` authenticates via `ANTHROPIC_OAUTH_TOKEN` and emits the expected json lifecycle stream before any agent is switched.

---

## 6. Failure modes + rollback

### Edge cases the bridge must handle (restated as failure policy, consistent with §3.3)

| Edge case | Detection | Action | Recoverable? |
|---|---|---|---|
| **Transient API error** (the ORIGINAL openclaude failure) | abort class: `stopReason=="error"` **OR** no `agent_end` **OR** watchdog kill (shape unverified — §2.1) | **bounded retry** (idempotent one-shot), then `mc blocked` if still failing — never `failed` | yes — human/redispatch from blocked |
| **Crash mid-turn** (SIGKILL/OOM) | `agent_end` absent when process exits | abort class → retry → `mc blocked` | yes |
| **Hung / wedged omp** (deadlock, TLS stall, internal infinite retry) | **bridge.py wall-clock + no-progress watchdog** SIGKILLs omp (§3.3) — `--max-time` alone is NOT trusted | abort class → retry → `mc blocked` | yes |
| **`--max-time` hit** | final `stopReason=="toolUse"` / `[Command cancelled]` | abort class → retry → `mc blocked` (task truncated) | yes |
| **Tool-approval hang** | shouldn't occur (`-p` cannot prompt, `--approval-mode yolo`); if a tool blocks, watchdog cancels | watchdog → `mc blocked` | yes |
| **Empty / no-op turn or malformed reflection** | `agent_end`+`stop` but no sentinel / reflection invalid | `mc blocked` (silent-abort / malformed branch) | yes |
| **Launch/preflight failure** (config/credential — deterministic, cf. err2-stream) | exit 1/2, no json session | `mc blocked` (retry would repeat it) | yes — human fixes config |
| **bridge.py itself dies** (post-ack, pre-terminal-PATCH) | **backend DB-only watchdog** (§3.5) | backend auto-PATCH `blocked` after ~90–120 s | yes — the authoritative net |
| **Agent sets blocker itself** | agent's own `mc blocked` already fired | idempotency guard suppresses a duplicate | n/a |

Note: **no row routes to `mc failed`.** Given the verified lifecycle (`FAILED → {INBOX}` only, auto-unassign, no auto-redispatch), every non-finish outcome above is more safely expressed as `blocked` (reversible, human-visible) or resolved by retry.

### Idempotency / double-reporting guard

- The omp image **drops `turn-state.sh` stagnation** entirely — the bridge is the single in-container source of turn-state, and the backend watchdog is the authoritative net.
- Before any terminal PATCH, the bridge checks the task's current status via `/me/poll`; if it is already `review|blocked|failed|done`, it **no-ops** (the agent or the backend watchdog beat it to it). Mirrors `mc ack`'s existing idempotency and `poll.sh`'s `LAST_BLOCKED_TASK_ID` guard.
- Backend already **hard-requires** `blocker_type` on `status=blocked` (422 otherwise, `agent_task_status.py:1602-1614`) — the bridge always sends `--blocker-type technical_problem`.

### Rollback story

- **Per-run:** a bad classification only ever produces a *blocker* (human-visible, reversible) — never a silent hang and never a dead `failed`. Worst case is a false-positive blocker a human clears in seconds.
- **Per-agent:** switch the agent's runtime row back → `switch_agent_runtime` `force_recreate`s onto the old image with full `_rollback`.
- **Whole feature:** one new image + one `pick_image_for_runtime` branch + one runtime row + the backend watchdog (which is a net positive for all runtimes and can ship independently). Not selecting the omp runtime = the harness is dormant; the backend watchdog still helps openclaude/host.

### Known risks to design around (from the runtime-architecture spike)

- **Health-check assumes a tmux ready-glyph.** Resolved by the topology in §3.1: bridge.py is the persistent pane process and prints `OMP_BRIDGE_READY`; the health check matches that stable anchor, not the loose `'$ '` shell prompt. Also revisit `HEALTH_TIMEOUT_RECREATE=90` — Claude cold-start + omp handshake may exceed it.
- **`pick_image_for_runtime` returns `None` for unknown runtimes** and `detect_image_change` treats `None` as "assume change" → the explicit `runtime_type == "omp"` branch is **mandatory** for correct selection.
- **`docker-compose.agents.yml` is generator-managed** — the anchor+image logic must live in `compose_renderer`, not the YAML.
- **Recycler** is **forked** (§3.1.1), not copied — it tracks `bridge.py`, never the one-shot omp subprocess.
- **Sessions live-terminal** shows the bridge.py pane with omp's teed NDJSON.

---

## 7. Prototype (this workflow) vs Phase-2 deployment (gated on Mark)

### What THIS prototype builds (non-destructive, only new files under `docker/omp-bridge/` + this doc)

- `docker/omp-bridge/Dockerfile` — installs pinned `omp` + tmux + python + the copied `mc` CLI.
- `docker/omp-bridge/entrypoint.sh` — 3-window tmux + bootstrap-token flow, exports `ANTHROPIC_OAUTH_TOKEN` from `CLAUDE_CODE_OAUTH_TOKEN`.
- `docker/omp-bridge/bridge.py` — the **persistent Window-0 driver**: poll loop, omp subprocess spawn, NDJSON reducer, **independent wall-clock watchdog**, completion gate (§3.3/§3.4), `OMP_BRIDGE_READY` health line, NDJSON tee to pane.
- `docker/omp-bridge/omp-recycler.sh` — **forked** recycler tracking `bridge.py` (§3.1.1).
- `docker/omp-bridge/tests/` — replay tests over captured NDJSON (finish, malformed-reflection, anti-echo, synthetic crash, synthetic hang — §3.4).
- `docker/omp-bridge/README.md` — build/run + the outcome-mapping table.
- `docs/omp-bridge-design.md` — this document.
- Captured evidence under `docker/omp-bridge/rpc/*.ndjson` — the ground-truth. **Explicitly missing and required before merge:** a real transient-error Claude stream and a live Claude finish stream (see gate #8).

The prototype is **not wired into the backend** and **not selectable** yet. It is reviewable in isolation and testable by replaying captured NDJSON through `bridge.py`.

### What Phase-2 deployment needs (each gated on Mark)

0. **Backend DB-only watchdog (MANDATORY, do first) — §3.5.** `in_progress + ack_at set + heartbeat idle N beats + no terminal PATCH since ack_at → auto-PATCH blocked after ~90–120 s`. This is the authoritative, runtime-agnostic net that survives a dead bridge.py and also closes the original openclaude/host gap. Ship as its own PR; it is a net win even if omp never lands.
1. **`compose_renderer.pick_image_for_runtime`** — add the `runtime_type == "omp"` → `OMP_IMAGE` branch **before** the anthropic-slug check + `x-omp-agent-base` anchor. *Mandatory for selection.*
2. **`runtime_seeder.py`** — seed an `omp` runtime row (`runtime_type == "omp"`, `model_identifier == claude-opus-4-8`, slug of your choice).
3. **`scripts/build-agent-images.sh`** — an `omp` build target that materializes `mc-cli/` into the image context.
4. **`docker_agent_sync.py` (MANDATORY, two parts):** (a) treat `runtime_type == "omp"` as **token-bearing** in `is_anthropic`/`is_anthropic_slug` — write `CLAUDE_CODE_OAUTH_TOKEN`, suppress `OPENAI_*` (§5.2); (b) omp readiness signal (`OMP_BRIDGE_READY`) for `wait_for_agent_healthy` and tuned health timeouts for Claude+omp cold-start.
5. **`internal.build_runtime_env` (MANDATORY):** dedicated `runtime_type == "omp"` branch emitting `CLAUDE_CODE_OAUTH_TOKEN` and **no** `OPENAI_*` shim (§5.2). Not optional — the slug-based path mis-routes auth.
6. **Mandatory per CLAUDE.md:** an **ADR** in `docs/decisions/` + a `docs/ARCHITECTURE.md` update.
7. **First live gate (hard, blocks any real switch):**
   - Run `omp -p --model claude-opus-4-8` end-to-end against the real Vault token; confirm the json stream + Claude auth.
   - **Capture a REAL transient-error stream** (§2.1): run against live Claude, blackhole/kill the endpoint mid-run, record the NDJSON; confirm which abort shape omp actually produces and that the bridge's abort-class routing (retry→blocked) handles it. *The gap is not claimed closed until this stream exists.*
   - **Sentinel reliability on live Claude** (§3.4): run real tasks and record **false-positive** (finish emitted without a true finish) and **false-negative** (clean finish that omits/echoes the sentinel or lands its conclusion in a thinking block) counts. The sentinel is the whole fix for the semantic silent-abort case and is **untested on Claude** — it must pass this gate before any agent is switched.
   - Replay all fixtures (finish / malformed-reflection / anti-echo / crash / hang) through `bridge.py`.
   - Then switch a **single throwaway canary agent** before any real agent.

**Loop guardrails honored:** every Phase-2 item is reversible (branches, opt-in runtime row), the merge decision stays Mark's, the switch is per-agent (blast radius = one agent), and item #0 (the backend watchdog) is a standalone reliability win that de-risks the rest.
