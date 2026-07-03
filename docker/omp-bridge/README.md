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

## The pieces

| File | What it is |
|---|---|
| `entrypoint.sh` | Container PID 1. Bootstraps tokens, renders `models.yml`, skips the omp setup wizard (`omp config set`), then boots the 3-window tmux: **Win0 = native TUI**, **Win1 = `bridge.py --serve`**, **Win2 = recycler**. |
| `launch-omp.sh` | Single source of truth for the native TUI invocation (`omp --hook … --model … --cwd …`). Used by the entrypoint (boot) and by `bridge.py` (per-task relaunch). Sources `omp.env` so a `tmux respawn-window` still gets provider/model. |
| `turn-end-hook.mjs` | ESM hook (`omp --hook`). Subscribes to omp lifecycle events and appends one JSON line per event (`session_start`/`turn_end`/`agent_end`/progress) to a signal file. The completion **oracle** — never throws, no-ops on missing fields. |
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
