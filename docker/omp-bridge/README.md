# omp-bridge — native-TUI omp runtime (`mc-omp-agent`)

Drives an MC agent with **omp** running as its **real, native, scrollable TUI**
in tmux Window 0 — the same session the Sessions page attaches to — instead of
the tmux screen-scrape harness (`openclaude` + `poll.sh` + `turn-state.sh`) or
the earlier headless `omp -p` one-shot. Completion is decided **deterministically
from a lifecycle hook** (never screen-scraping), and mapped to the MC agent
lifecycle (`mc ack` / `mc finish` / `mc blocked`).

**It closes the silent-abort gap:** every run resolves into exactly one of
`{finish, blocker}` — there is no path that ends a run and leaves the task
`in_progress`. A hang, a dead TUI, or a per-task deadline all trip a watchdog
that SIGKILLs + relaunches the TUI and blocks the task.

Design + rationale: **[ADR-049](../../docs/decisions/049-omp-native-tui-session.md)**
(supersedes the headless drive model of
[ADR-045](../../docs/decisions/045-omp-runtime.md)).

---

## ⚠️ Contract for ANY agent harness (don't forget when integrating a new CLI)

A harness that replaces `poll.sh` (like this one) **loses two things poll.sh did
for free** for the claude/openclaude fleet. Every new CLI bridge (a future
coding agent, a new TUI, etc.) MUST re-provide both, or its agents silently
break in ways that look like model problems:

1. **Write `/tmp/mc-context.env` on every dispatch.** The `mc` CLI reads
   `TASK_ID` / `BOARD_ID` / `X_DISPATCH_ATTEMPT_ID` from this file first
   (`scripts/mc-cli/mc_cli/config.py:from_env`). Without it the agent's own
   `mc ack|deliverable|done` fail (*"TASK_ID … müssen gesetzt sein"*) and status
   calls are rejected 409 (*"Missing X-Dispatch-Attempt-Id"*) → the agent can't
   register deliverables. `poll.sh` writes it (`docker/shared/poll.sh`); here
   `bridge.py:write_task_context_env` does. Fallback: `mc recover` rewrites it.

2. **Emit a streaming progress heartbeat, not just turn/tool-boundary events.**
   The no-progress watchdog (`OMP_TURN_IDLE_TIMEOUT`, default 900s; the backend hands slow local
   runtimes 1800s) measures liveness
   from the hook's `progress` records. If the harness only emits at
   `turn_start` / `tool_execution_end`, a **single long generation** (e.g. the
   model writing a 2000-line file in one tool call — no `tool_execution_end`
   until the args finish) looks like a hang and gets SIGKILLed mid-write. The
   hook must heartbeat on a per-delta event (here: `message_update`, throttled —
   see `turn-end-hook.mjs`). Verify empirically: `progress @ <delta-event>`
   records must appear in the signal file during a long generation.

   The second watchdog is the **per-task wall clock** (`OMP_TASK_DEADLINE`,
   default 3600s; the backend hands slow local runtimes 7200s). It is only the
   runaway guard — a task that is still streaming when it expires is killed too.
   03.09.2026: a working 20-minute audit died at the old 1200s default while
   heartbeating every 3s. `classify()` now names the trigger (deadline / idle /
   child_dead) in the blocker text instead of one generic "kein Stream-Fortschritt".

3. **A running tool is alive — and must say so.** The delta heartbeat (2.)
   covers *generation*; it is silent while a tool *executes*. One `bash` call
   running a 17-minute test suite produced no event between
   `tool_execution_start` and `_end`, and the idle watchdog (600s on local
   models) killed a working agent (04.09.2026). The hook therefore emits
   `tool_start` / `tool_end` (with `toolName` + `toolCallId`) and, while ≥1
   tool is in flight, a timer heartbeat `progress @ tool_heartbeat` every
   `OMP_TOOL_HEARTBEAT_MS` (30s). The timer runs on omp's event loop, so it
   proves the *process* is alive — a TUI that wedges inside a tool still
   trips the idle watchdog, and the blocker then names the tool. The wall
   clock (`OMP_TASK_DEADLINE`) is untouched: a tool that never ends is still
   stopped. Any other harness with an idle watchdog needs the same three
   signals: tool start, periodic liveness while running, tool end.

4. **An open model request is alive — before the first token.** Measured on
   omp 16.4.6 against a local GLM-5.3 with a 26k-token prompt: between
   `turn_start` and the assistant's `message_start` there were **58 s with no
   event at all** (prefill). Hidden reasoning or a bigger context stretches
   that to minutes — still no delta, still no tool, so 2. and 3. are both
   silent. The hook therefore treats the request window
   `turn_start → assistant message_end` like a running tool: `model_start` /
   `model_end` records plus `progress @ model_heartbeat` on the same timer
   (`OMP_TOOL_HEARTBEAT_MS`). User / toolResult messages do not touch the
   window; `turn_end` / `agent_end` / `session_start` close it. If omp
   freezes inside a request the idle watchdog still fires and the blocker
   says „Modell-Anfrage offen" (`watchdog_phase == "model"`). As a safety net
   the idle defaults moved 300→900 s (cloud) and 600→1800 s (local) on the
   same day — a guessed number must never kill a thinking model.

1.–2. were live bugs on the omp path — fixed in PR #68; 3. and 4. followed on 04.09.2026. See also
`mc_cli/config.py` (the file contract) and `bridge.py:supervise_stream` /
`run_native_turn` (the watchdog).

---

## The pieces

| File | What it is |
|---|---|
| `entrypoint.sh` | Container PID 1. Bootstraps tokens, renders `models.yml`, skips the omp setup wizard (`omp config set`), then boots the 3-window tmux: **Win0 = native TUI**, **Win1 = `bridge.py --serve`**, **Win2 = recycler**. |
| `launch-omp.sh` | Single source of truth for the native TUI invocation (`omp --hook … --model … --cwd …`). Used by the entrypoint (boot) and by `bridge.py` (per-task relaunch). Sources `omp.env` so a `tmux respawn-window` still gets provider/model. |
| `turn-end-hook.mjs` | ESM hook (`omp --hook`). Subscribes to omp lifecycle events and appends one JSON line per event (`session_start`/`turn_end`/`agent_end`/`tool_start`/`tool_end`/progress) to a signal file. The completion **oracle** — never throws, no-ops on missing fields. |
| `bridge.py` | The heart. `serve_loop` polls `/me/poll`; per task it relaunches Window 0 with the task cwd, injects the dispatch as an `@file` mention via `tmux send-keys`, tails the hook signal, folds it into a `RunOutcome`, and runs the **unchanged** `classify()` → `decide_lifecycle()` → `McCliLifecycle` (ack/finish/blocked + finish→blocked fallback). Includes the SIGKILL watchdog. |
| `omp-recycler.sh` | Window-2 recycler. Keeps **both** the TUI (Win0) and the bridge (Win1) alive; only touches the TUI when idle (the bridge owns it during a task). |
| `tests/test_bridge.py` | Golden tests for the NDJSON reducer/classifier (real captured streams). |
| `tests/test_serve_loop.py` | Poll loop: ack-dedup, idle-clear, retry→blocker, ready sentinel. |
| `tests/test_native_tui.py` | The native driver: hook-signal→outcome mapping, `@file`/Escape/Enter inject, per-task relaunch isolation, the SIGKILL watchdog, drain/offset primitives, and end-to-end through `drive_live_run`. |
| `Dockerfile` | The `mc-omp-agent` image (omp binary + tmux + mc CLI + hook + launcher + bridge). |
| `rpc/*.ndjson` | Real captured omp streams = ground truth for the reducer tests. |

---

## Run the tests

```bash
cd docker/omp-bridge
python3 -m pytest tests/ -q            # 44 tests
# or standalone (no pytest):
python3 tests/test_bridge.py
python3 tests/test_serve_loop.py
python3 tests/test_native_tui.py
```

---

## Turn signal → MC lifecycle (the core mapping)

The decision comes from the **hook signal**, never pane text. `mc failed` is
**never** used — `blocked` is reversible and human-visible.

| Hook signal (this task's turns) | Reduced to | MC action |
|---|---|---|
| `session_start` / `hook_ready` after relaunch | ready | (inject the `@file` task) |
| `turn_end stopReason=toolUse` / `length` | agent continues | keep waiting |
| `turn_end stopReason=stop` + `TASK_COMPLETE` + valid 4-field reflection | `finish` | `mc finish [--review]` |
| `turn_end stopReason=stop` but no sentinel | `silent_abort_no_sentinel` | `mc blocked` |
| `turn_end stopReason=error`/`aborted` | error family | retry ×N → `mc blocked` |
| no terminal turn by deadline / idle / TUI child dead | `abort_hang` (watchdog SIGKILL + relaunch) | retry ×N → `mc blocked` |

Verified in-container against real Qwen (`nvidia/Qwen3.6-35B-A3B-NVFP4`): the TUI
boots straight to chat, a task injected via `@file` runs visibly and yields a
`stop` turn → `finish`; a per-task deadline trips the watchdog → SIGKILL +
relaunch → `blocked`.

---

## Ship note

Same-image rework: rebuild `mc-omp-agent` (`scripts/build-agent-images.sh omp`)
and restart the omp agent (same-image respawn). One backend follow-up is
required for the switch health-gate — see ADR-049 §7 (the omp `ready_signals`
re-anchor from `OMP_BRIDGE_READY` to the TUI chat glyph).
