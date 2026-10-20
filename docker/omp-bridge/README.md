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

**Delivered is not working ([ADR-097](../../docs/decisions/097-delivered-is-not-working.md)).**
On the ACP driver (`OMP_DRIVER=acp`) the bridge claims a task (`mc ack` →
in_progress) only on the model's first work — a thought, a tool call, a second
text chunk, or a turn that ended normally with text (`FirstActivityAck`,
`RunOutcome.model_worked`). Until then the card stays `inbox` (delivered), the
bridge holds its own comments, and a run whose model never answered is blocked
straight from inbox. Deploy the backend first: it must know that a heartbeat
for a delivered, unclaimed card is live work. The native TUI path keeps the
up-front ack.

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
| `entrypoint.sh` | Container PID 1. Bootstraps tokens, renders `models.yml`, skips the omp setup wizard (`omp config set`), then boots the tmux layout: **Win0 = native TUI**, **Win1 = `bridge.py --serve`**, **Win2 = recycler** — plus **Win3 = `acp_chat.py --serve`** when `OMP_DRIVER=acp`. Under `OMP_DRIVER=acp`, Win0 does NOT run the TUI (fix omp-acp-no-tui-window, 13.09.2026): it prints an `OMP_ACP_READY` banner and drops to a plain shell — tasks and chat both run over ACP, so a live TUI there would be an idle ghost session. |
| `launch-omp.sh` | Single source of truth for the native TUI invocation (`omp --hook … --model … --cwd …`). Used by the entrypoint (boot) and by `bridge.py` (per-task relaunch). Sources `omp.env` so a `tmux respawn-window` still gets provider/model. |
| `turn-end-hook.mjs` | ESM hook (`omp --hook`). Subscribes to omp lifecycle events and appends one JSON line per event (`session_start`/`turn_end`/`agent_end`/`tool_start`/`tool_end`/progress) to a signal file. The completion **oracle** — never throws, no-ops on missing fields. |
| `bridge.py` | The heart. `serve_loop` polls `/me/poll`; per task it relaunches Window 0 with the task cwd, injects the dispatch as an `@file` mention via `tmux send-keys`, tails the hook signal, folds it into a `RunOutcome`, and runs the **unchanged** `classify()` → `decide_lifecycle()` → `McCliLifecycle` (ack/finish/blocked + finish→blocked fallback). Includes the SIGKILL watchdog. |
| `acp_chat.py` | Chat daemon for `OMP_DRIVER=acp` (Win3). Holds ONE long-lived `omp acp` child and ONE ACP session — resumed via `session/load` after a restart, a recreate or a crash of the omp child, so the chat history keeps growing in the same transcript file, **as long as the model is the same as at the conversation's last turn**; otherwise (omp cannot restore that model) a new conversation and a visible `session_reset` card. The pointer to the open conversation, `acp-chat.json`, sits next to `acp-chat-state.json` in the session folder on the mount (since S1-R4; before, in the unmounted `$OMP_HOME`, so each recreate started the chat over silently); a state mirror that is newer than the pointer and names another session wins (a rollback, a failed pointer write) — what survives what: [chat-over-acp spec](../../docs/specs/chat-over-acp.md#what-survives-what-s1-r4-20102026). Writes the user line, the streamed preview and exactly ONE final assistant line through `acp_chat_events` — into `<session folder>/.mc/` (transcript copy, `previews/`, `media/`), where omp never looks — mirrors `configOptions`/`commands` to `acp-chat-state.json`, and turns every failure into a visible `chat_error` line (`rpc_error`, `provider_error`, `empty_turn`, `process_exit`, `turn_interrupted`, `model_unreachable` — the model's endpoint does not answer, read from omp's own error line in its session file, no reply bubble then (S1-R6) —, `busy`, `session_reset` with `detail` `transcript_missing` / `model_missing` (the model's name also as `data.model`) — `cancelled` is NOT an error; a start without any pointer but with an earlier chat copy in the tree says so with the notice `chat_started_fresh`). Serves the control protocol on `$OMP_HOME/acp-chat.sock`. |
| `sink_migration.py` | Moves MC's display copies of ACP sessions out of omp's sight (S1-R3): every `*.jsonl` with `bridge: "acp"` in its header that still sits next to omp's own files goes to `<folder>/.mc/`, its pictures from `media/` to `.mc/media/`. Never deletes, never overwrites, never touches subfolders, dot files or omp's files, never follows a symlink out of the root (the tree is the agent's own mount, the host run has the host's rights); lists the moves in `<root>/.mc-sink-migration.json`; `--rollback [--all]` undoes it (`--all` also with a damaged list). Runs on the host in the deploy window (`python3 sink_migration.py --root <sessions root> --dry-run` / `--apply`) and, as a safety net, in `acp_chat.py` at start. Standard library only, Python 3.9+. Why: [chat-over-acp spec, "Storage"](../../docs/specs/chat-over-acp.md). |
| `model_errors.py` | "The model is not answering" — one rule source for the task path and the chat (S1-R6): `MODEL_UNREACHABLE_RE` and `UNREACHABLE_TAIL_CHARS` (imported by `bridge.py`), the parser for the line that ends a turn in omp's own session file (`stopReason: "error"` + `errorMessage`, retried attempts and aborts told apart) and the chat's decision (`classify_chat_turn`: card `model_unreachable`, hide the reply, abort, or nothing). Standard library only, Python 3.9+ (the host's Hermes daemon imports it through `acp_chat.py`). Why: [chat-over-acp spec, "The model is not answering"](../../docs/specs/chat-over-acp.md). |
| `acp_chat_ctl.py` | The CLI in front of that socket — how the backend reaches the chat (`docker exec … acp_chat_ctl.py <op>`). Exit **0** = `ok`, **2** = `ok:false`, **3** = socket unreachable. |
| `omp-recycler.sh` | Window-2 recycler. Keeps **both** the TUI (Win0) and the bridge (Win1) alive; only touches the TUI when idle (the bridge owns it during a task). Under `OMP_DRIVER=acp` the TUI-relaunch responsibility is disabled outright — Win0 has no TUI to relaunch, just the static banner shell. |
| `cdp-relay.sh` | omp's built-in browser tool → the shared agent browser (`cdp-browser`, Chromium with WebGL), via the agent-aware `cdp-gateway` (bauplan.md PR B1). omp's own Chrome launch can never work on arm64, and Chromium rejects a service name in the Host header, so a socat relay on `127.0.0.1:9222` inside the container forwards to `cdp-browser:9300` (resolved per connection — survives a recreated `cdp-browser`). The gateway identifies this agent by the connection's source IP (reverse DNS → `mc-agent-<slug>`), so its tabs are attributed to this agent in the operator's per-agent browser panel. `render-omp-config.sh` sets `browser.cdpUrl=http://127.0.0.1:9222` only when the target resolves, else resets it. Env: `OMP_BROWSER_CDP_TARGET` (default `cdp-browser:9300`; `cdp-browser:9223` bypasses the gateway and loses per-agent attribution; `off` disables), `OMP_BROWSER_CDP_PORT` (default 9222). Started by the entrypoint and re-checked by its watchdog. Whatever omp opens there shows up in the operator's browser panel. |
| `tests/test_acp_chat.py` | The chat daemon: turn shape, busy rejection, cancel-is-not-an-error, config/state mirror, `session/load` append + reset, error codes, socket round-trip through the ctl shim. |
| `tests/test_acp_chat_model_unreachable.py` | A lost model in the chat: omp's error line → the card instead of a reply, a real reply kept, aborts and 401 untouched, the text rule without omp's file, the turn mark and the late flush, "Send again" only for the operator's text, and the parity table against the task path (#897). |
| `tests/test_sink_location.py`, `tests/test_sink_migration.py` | Where MC's copy lives (checked against `tests/omp_listing_rule.py`, a Python twin of omp 18.10.1's listing rule) and the move: list, lock, rollback, the daemon's start-up safety net. |
| `tests/test_dockerfile_copies_modules.py`, `tests/test_py39_compat.py` | Every module the bridge imports is copied into the image; the modules the host loads stay Python 3.9-compatible. |
| `tests/test_bridge.py` | Golden tests for the NDJSON reducer/classifier (real captured streams). |
| `tests/test_serve_loop.py` | Poll loop: ack-dedup, idle-clear, retry→blocker, ready sentinel. |
| `tests/test_native_tui.py` | The native driver: hook-signal→outcome mapping, `@file`/Escape/Enter inject, per-task relaunch isolation, the SIGKILL watchdog, drain/offset primitives, and end-to-end through `drive_live_run`. |
| `Dockerfile` | The `mc-omp-agent` image (omp binary + tmux + mc CLI + hook + launcher + bridge). |
| `rpc/*.ndjson` | Real captured omp streams = ground truth for the reducer tests. |

### What lives where (and survives a recreate)

`$OMP_HOME` is `/home/agent/.omp` inside the container. Only the folders
below are mounted from the host (`backend/app/services/compose_renderer.py`
writes the lines); everything else in `$OMP_HOME` is gone after a recreate
and is rendered again at boot.

| Inside the container | On the host | Survives a recreate | What |
|---|---|---|---|
| `$OMP_HOME/profiles/mc-agent/agent/sessions/` | `~/.mc/agents/<slug>/omp-sessions/` | yes | omp's session files; per session folder MC's copies in `.mc/`, the state mirror `acp-chat-state.json` and the chat pointer `acp-chat.json` (S1-R4) |
| `$OMP_HOME/profiles/mc-agent/agent/blobs/` | `~/.mc/agents/<slug>/omp-blobs/` (rw, S1-R8) | yes, with S1-R8 | omp's pictures of a conversation (`blob:sha256:<hash>`); before S1-R8 a recreate kept the conversation but lost its pictures |
| the rest of `$OMP_HOME`: `omp.env`, `profiles/mc-agent/agent/models.yml`, `acp-chat.sock` | — | no | rendered again at boot (`render-omp-config.sh`) or created by the daemon |

---

## Run the tests

```bash
cd docker/omp-bridge
python3 -m pytest tests/ -q            # the whole suite
# or standalone (no pytest):
python3 tests/test_bridge.py
python3 tests/test_serve_loop.py
python3 tests/test_native_tui.py
python3 tests/test_acp_chat.py
```

### Driving the ACP chat by hand

```bash
docker exec mc-agent-<slug> python3 /opt/omp-bridge/acp_chat_ctl.py state
docker exec mc-agent-<slug> python3 /opt/omp-bridge/acp_chat_ctl.py prompt --json '{"text": "hello"}'
docker exec mc-agent-<slug> python3 /opt/omp-bridge/acp_chat_ctl.py config --json '{"id": "thinking", "value": "high"}'
docker exec mc-agent-<slug> python3 /opt/omp-bridge/acp_chat_ctl.py cancel
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
