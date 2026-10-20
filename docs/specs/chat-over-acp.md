# Chat over ACP — headless chat for ACP-driven agents

Status: accepted 2026-09-13 (Mark: "alle Funktionen über die Chat-UI, kein Terminal-Tab bei ACP-Agenten"; Boss: Fehler müssen als sichtbares Ereignis im Chat landen).

## Problem

Agents with `OMP_DRIVER=acp` run their **tasks** through `omp acp` (one process per attempt, `bridge.run_acp_once`), but the Sessions **chat** still types into the native TUI in tmux window 0 (`agent_chat_input.send_text` → `tmux send-keys`). Two consequences:

1. The chat and the task path are two different brains — what the operator types never reaches the ACP session, and the Terminal view shows an idle second console.
2. Everything the composer offers (effort chip, `/model`, slash palette, Stop) is implemented as key presses against a TUI that, for an ACP agent, should not exist.

Hermes (host runtime) has no chat at all: the bridge drives a `hermes --yolo` TUI in tmux and the Sessions page shows nothing.

## Goal

For an agent whose driver is ACP, the Sessions chat is the **only** interface and it is complete: prompts, streaming preview, cancel, effort (thinking level), model switch, slash commands, and **errors** all travel over ACP. The Chat/Terminal toggle is not rendered for such agents (`headless_chat`). The same contract serves omp (container) and Hermes (host).

## Non-goals

- Multi-turn concurrency. One turn at a time per agent chat session. A second prompt while busy is rejected with `busy` — unless it asks for `mode: "queue"` (the chat does): then the daemon holds it and runs it when the running turn ends (FIFO, see the op table). Steering INTO a running turn is not possible over ACP: omp cancels the running turn when a second `session/prompt` arrives (omp 18.1.10 `AcpAgent.prompt`); steering needs omp's RPC mode (`steer`/`follow_up`).

Done, no longer a non-goal (fix omp-acp-no-tui-window, 13.09.2026): the native
TUI in window 0 was removed for `OMP_DRIVER=acp` agents — Mark saw the live
TUI as an idle "ghost" session in the Terminal view and does not want that.
Window 0 itself still exists (Boss: keep the Terminal reachable until the
live proof) but shows an `OMP_ACP_READY` banner + a plain shell instead of
launching omp; the health gate and the recycler were updated to match (see
`docker/omp-bridge/entrypoint.sh` / `omp-recycler.sh`, and
`agent_runtime_switch.OMP_READY_SIGNALS` on the backend side). `?view=terminal`
still works as a deep link.

## Architecture

```
Sessions chat (frontend)
   │  POST /agents/{id}/chat/input | keys | effort | GET history
   ▼
backend  agent_chat_input.py ── headless_chat? ──► ChatTransport
   │                                              ├─ DockerCtlTransport  (docker exec mc-agent-<slug> python3 /opt/omp-bridge/acp_chat_ctl.py <op>)
   │                                              └─ HttpCtlTransport    (POST http://host.docker.internal:18794/chat/<op>)   ← Hermes
   │
   ▼ reads (mount ~/.mc/agents/<slug>/omp-sessions/<encoded-cwd>/)
.mc/*.jsonl (transcript copy)  ·  .mc/previews/*.jsonl  ·  .mc/media/  ·  acp-chat-state.json
   ▲
   │ writes
acp_chat.py --serve   (long-lived chat daemon; one `omp acp` / `hermes acp` child, one ACP session, session/load on restart)
   ▲ Unix socket  $OMP_HOME/acp-chat.sock  (JSON lines request/response)
acp_chat_ctl.py <op>  (CLI shim used by docker exec)      ·      hermes-bridge.py /chat/<op> (HTTP shim on the host)
```

### Component: `docker/omp-bridge/acp_chat.py` (chat daemon)

- `ChatSession(client_factory, cwd, state_dir, sessions_dir)` — pure-Python core, testable with a fake ACP process (`tests/fake_acp_server.py`) or an in-memory fake client.
- Startup: move MC copies an older image left next to omp's files into `.mc/` (see "Storage" below); read `state_dir/acp-chat.json` (`{"sessionId", "transcript"}`) and follow a pointer to a moved file (`sink_migration.remap`); `initialize`; try `session/load` with the stored id → on success append to the stored transcript file (history survives restarts); on failure `session/new`, new transcript file, and emit `chat_error{code:"session_reset"}`. When the move did not run (lock held by a live run, unreadable list, error) the chat shows a `chat_notice` with `code: "sink_migration_skipped"` and the chat starts anyway.
- Uses `acp_chat_events.ACPEventMapper` + `ChatEventSink` + `PreviewEventSink` exactly like `run_acp_once` (user line via `map_user_prompt`, streaming previews, ONE final assistant line with usage).
- Thinking (20.10.2026): a thought's preview line carries `data: {"block": "thinking", "chars": <full size>, "startedAt": <iso>}` and only the END of the thought as `content` (≤ 2 000 chars — the file grows linearly). The backend passes these on the `preview` SSE event; the chat shows "Thinking · ≈N tokens · m:ss" instead of an answer preview. When the thought ends (next answer chunk, tool call, plan, the turn's final line or a chat error) ONE assistant line with a `{"type": "thinking", "thinking", "durationMs"}` block lands in the transcript. Usage/config updates do not end a thought.
- Running task in the agent chat (20.10.2026): the agent chat shows the chat daemon's session only (#898); a task runs in a session of its own (bridge header `taskId`). `GET /agents/{id}/chat/task-activity` reads that session through the same adapter (`find_task_session`, `tasks_in_own_session`) and the chat renders it as a separate "From the task" block — delivery, the newest steps, the running thought, more inbox tasks. Harnesses whose task runs inside the chat answer `applies: false` — Claude Code, omp under the native driver, and Hermes (over ACP the hermes-bridge hands a task to the chat daemon itself).
- `configOptions` (from `session/new`/`session/load` result and `config_option_update` notifications) and `available_commands_update` are kept in memory and mirrored to `sessions_dir/acp-chat-state.json` after every change.
- Permissions: `OMP_ACP_PERMISSIONS` policy (yolo → allow), same helper as the bridge.
- Errors → transcript `custom_message` line, `customType: "chat_error"`, content = human text, plus `data: {"code": ..., "detail": ...}`. Codes: `rpc_error` (JSON-RPC error on prompt/config), `provider_error` (`401`/`403`/`429`/quota text detected in the error or in the final agent text when the turn produced no text), `empty_turn` (turn ended with stopReason `end_turn` and zero agent text), `process_exit` (child died; daemon restarts it and re-loads the session), `busy`, `session_reset`, `queue_full` (20 messages already held), `new_session_failed` (`session/new` refused; the current session continues), `queue_lost` (a restart dropped held jobs), `cancelled` is NOT an error (it produces a normal end-of-turn).
- Model pin (incident 19.10.2026): every `session/new` and `session/load` pins the selector read from the CURRENT `omp.env` (`acp_model.current_selector` — a runtime switch re-renders that file in the running container; the daemon's boot env goes stale). A selector the session does not offer is replaced by the offered model of the same provider and the chat gets a `custom_message` line `customType: "chat_notice"`, `data: {"code": "model_fallback", "previous", "current"}` (frontend `ChatNoticeCard`, `chat.notice.*`). The task path (`bridge.run_acp_once`) does the same, writes a refused pin into the task's chat as `chat_error`, and marks its prompts `attribution: "mc"` + `origin: {kind: "task"|"nudge"}` so the chat shows them as task events, not as operator bubbles.
- Socket server: `$OMP_HOME/acp-chat.sock` (path configurable via `--socket`), newline-delimited JSON; each connection = one request/one response.

### Storage: MC's copy lives in `<folder>/.mc/` (S1-R3, 20.10.2026)

A session folder (`<sessions root>/<encoded cwd>/`, e.g. `--workspace--`) holds
omp's own session files and MC's state mirror `acp-chat-state.json`. Everything
else MC writes for the chat lives one level deeper:

| What | Where |
|---|---|
| transcript copy (`ChatEventSink`, the file the chat view reads) | `<folder>/.mc/<ts>_<sessionId>.jsonl` — header unchanged (`type`, `version`, `id`, `timestamp`, `cwd`, `bridge: "acp"`, `taskId`) |
| preview channel (`PreviewEventSink`) | `<folder>/.mc/previews/` |
| tool pictures (`write_media`) | `<folder>/.mc/media/` (next to the transcript copy) |
| state mirror | `<folder>/acp-chat-state.json` (unchanged) |

**Why a folder, and why this one.** omp 18.10.1 treats every `*.jsonl` whose
first line (or second, after a title line) is `{"type": "session", "id":
<string>}` as one of its sessions. It lists them with `Bun.Glob("*.jsonl")`
per folder (`SessionManager.list`) and `Bun.Glob("*/*.jsonl")` over the root
(`listAll`), and load / resume / fork take the **younger** of two files with
one id. MC's copy carries exactly that header, so before S1-R3 omp listed
every chat session twice and often loaded MC's copy instead of its own file
(live 20.10.2026: "ACP session load was cancelled" for the current chat; the
daemon then started a new session without a word). `<folder>/.mc/x.jsonl`
lies two levels below the root, outside both globs; the leading dot also
keeps it out of a recursive search (Bun skips dot names unless asked).
Changing the header would mean rewriting old files, and a new extension would
change every `*.jsonl` reader — moving the file changes neither.

**Contract.** `acp_chat_events.MC_SINK_DIRNAME` and the backend's
`omp_chat._MC_SINK_DIRNAME` are both `".mc"` (checked as text by
`backend/tests/test_mc_sink_dirname_contract.py` — backend and bridge share no
code). `acp_chat_events.find_sink(folder, sessionId)` returns MC's copy:
`.mc/` first, then the old place, never omp's own file (only a header with
`bridge: "acp"` counts). The backend reads both places, so it must be
deployed before a bridge that writes to `.mc/`. The chat daemon looks up
tool names in omp's own file in the session folder and never takes a file
with MC's header for it.

**Moving old copies** — `docker/omp-bridge/sink_migration.py` (standard
library only, Python 3.9+, the same file in the image and in a checkout):

- In each folder directly under the sessions root (no dot folders, no
  symlinks) every `*.jsonl` directly in the folder with `bridge: "acp"` in its
  header moves to `<folder>/.mc/<same name>`; `<folder>/media/*` moves to
  `<folder>/.mc/media/`. One rename each, on the same file system, so the
  modification time stays (the backend ranks sessions by it).
- Never: delete, overwrite (an existing target is skipped with a reason),
  touch a subfolder (`previews/`, omp's artifact folders — also those with
  MC's name pattern), a dot file (omp's `.<name>.jsonl.lock.os`),
  `acp-chat-state.json`, `acp-chat.json` or omp's files, or create a `.bak`
  name (omp renames `*.jsonl.<x>.bak` back into sessions).
- A copy omp also wrote into ("mixed file", three on the fleet from
  16.09.2026) moves like any other and is marked `mixed` with the count and
  types of omp's lines.
- The list `<root>/.mc-sink-migration.json` (never `.jsonl` — the backend's
  `*.jsonl` search would read it as a session) records every run that moved
  something and every command-line run: `{from, to, sessionId, task, mixed}`
  per file, paths relative to the root (host and container see different
  absolute paths). It is rewritten atomically, written before the first move
  and again after the last, so a crash in between stays traceable.
- The lock `<root>/.mc-sink-migration.lock` (`O_EXCL`) holds pid, time and
  host. Older than 10 minutes, or of a dead process on the same host → taken
  over; otherwise the run stops with `locked`.
- `rollback` moves each listed file back when its old place is free;
  `--all` also moves back copies written into `.mc/` after the move, and
  `.mc/media/*`. `remap(path)` sends an old pointer to the moved file (and a
  pointer into `.mc/` back after a rollback).
- Command line: `python3 sink_migration.py --root <sessions root>
  (--dry-run | --apply | --rollback [--all]) [--json]`; exit 0 ok, 1 error,
  3 locked.

**When it runs.** The real move runs on the host in a deploy window, with the
agent stopped and no task in progress: `--dry-run` (counts only), then
`--apply`, numbers checked, only then the new image and the recreate. The
chat daemon runs the same move at every start, before it reads its pointer —
only a safety net. Switched-off agents move on their next **recreate** with
the new image, not on `docker start` (that starts the old image's code); the
backend reads both places until then. Hermes moves its copies at the next
start of its chat daemon. Way back: stop the agent, `--rollback --all` on the
host, then the previous image.

### Control protocol (socket and HTTP share the schema)

| op | request | response |
|----|---------|----------|
| `prompt` | `{"op":"prompt","text":"...","mode"?:"now"\|"queue"}` | `{"ok":true,"turn":n}` immediately (turn runs async) · busy + `mode:"queue"`: `{"ok":true,"queued":true,"queueId":"…","position":n,"at":"…"}` · busy otherwise: `{"ok":false,"error":"busy"}` · `{"ok":false,"error":"queue_full"}` |
| `new_session` | `{"op":"new_session","mode"?:"now"\|"queue"}` | `{"ok":true,"previousSessionId":"…"}` — `session/new` runs in the worker; a new transcript file starts (the chat view follows it as a rollover), the old file stays, model + thinking carry over, the old ACP session gets `session/close`. Busy: `{"ok":false,"error":"busy"}`, or held in the queue with `mode:"queue"` |
| `queue_clear` | `{"op":"queue_clear"}` | `{"ok":true,"dropped":[{"id","kind":"prompt"\|"new_session","text","at"}, …]}` oldest first |

The queue lives in the daemon's memory only. A daemon restart (container recreate, bridge reload) drops held jobs; the restarted daemon reads the old `queue` from the state mirror and posts one `queue_lost` card listing them. The composer shows held messages from its own optimistic echo, so after a page reload or on another device they are not shown until they run (follow-up: render them from the state file's `queue`).
| `cancel` | `{"op":"cancel"}` | `{"ok":true}` (no-op when idle) |
| `config` | `{"op":"config","id":"thinking","value":"high"}` | `{"ok":true,"configOptions":[...]}` / `{"ok":false,"error":"rpc_error","detail":"..."}` |
| `state` | `{"op":"state"}` | `{"ok":true, ...state}` |
| `answer` (P3) | `{"op":"answer","id":"el-…","action":"accept"\|"decline"\|"cancel","content"?:{…}}` | `{"ok":true,"status":"answered"\|"declined"\|"cancelled"}` · `{"ok":false,"error":"not_pending"}` (answered elsewhere, turn over, daemon restarted) · `{"ok":false,"error":"bad_content"\|"bad_action","detail":"…"}` (checked against the question's schema before omp sees it) |

**Question cards and plan mode (chat package P3).** Only the omp container's
chat daemon advertises `clientCapabilities.elicitation.form` (`ChatSession(...,
elicitation=True)` in `build_session`); task runs (`bridge.run_acp_once`) and
the host-side chat bridge do not, so an unattended run never stops on a question.
omp then asks through `elicitation/create` (form mode): the daemon keeps the
JSON-RPC request open under its own id `el-<12 hex>`, writes a
`custom_message`/`acp-elicitation` line (`content` = question text, `data =
{elicitationId, message, schema}` with the schema cut down to the rendered
fields) and lists the id in the state file's `elicitations`. The `answer` op
replies to omp and writes a `custom`/`acp-elicitation-result` line (`data =
{elicitationId, status, content?}`). A question still open when its turn ends
(omp's own timeout — form mode has no `elicitation/complete` — or Stop), on
`/new` or a child restart is answered `cancel` and marked `expired`; ids a
previous daemon still listed are marked `expired` at start. URL-mode requests
and unusable schemas are declined at once. Plan mode needs no op of its own:
`config` with `{"id":"mode","value":"plan"|"default"}`; `current_mode_update`
updates the `mode` row of `configOptions` and writes a quiet `custom`/`acp-mode`
line (`data = {modeId}`); `session_info_update` is mirrored as `sessionInfo`.
Backend: `POST /agents/{id}/chat/elicitation/{elicitation_id}` (admin, 409
`elicitation_not_pending`), `POST /agents/{id}/chat/mode` (admin; only values in
`capabilities.modeOptions`, which lists modes only when the agent reports at
least two).

`state` / `acp-chat-state.json` schema:

```json
{
  "version": 1,
  "driver": "omp" | "hermes",
  "sessionId": "…",
  "busy": false,
  "turn": 12,
  "transcript": "/abs/path/…jsonl",
  "configOptions": [{"id":"thinking","category":"thought_level","currentValue":"medium","options":[{"value":"off","name":"Off"}, …]}, {"id":"model","category":"model","currentValue":"…","options":[…]}, {"id":"mode", …}],
  "commands": [{"name":"usage","description":"…","input":null}, …],
  "queue": [{"id":"…","kind":"prompt" | "new_session","text":"…","at":"…"}, …],
  "updatedAt": "2026-09-13T12:00:00Z",
  "lastError": null | {"code":"…","detail":"…","at":"…"}
}
```

Slash commands are sent as prompt text (`/usage`, `/model X`); omp executes them inside `session/prompt`. Exception: `/new` and `/clear` — omp does not advertise them over ACP, so the backend sends them as `new_session` (queued behind a running turn) and the slash palette lists them; a harness that advertises its own `new`/`clear` keeps it. `/model` is additionally offered via `config id=model` so the composer's model picker uses the option list.

### CLI shim `acp_chat_ctl.py`

`python3 /opt/omp-bridge/acp_chat_ctl.py <op> [--json '{...}']` → prints the daemon's JSON response, exit 0 when `ok`, 2 when `ok:false`, 3 when the socket is unreachable (`{"ok":false,"error":"unreachable"}`).

### entrypoint (omp image)

When `OMP_DRIVER=acp`, `start_native` additionally opens tmux window 3 `win3`: `exec python3 /opt/omp-bridge/acp_chat.py --serve`. Window 0 still exists but no longer runs the native TUI (fix omp-acp-no-tui-window, 13.09.2026) — it prints an `OMP_ACP_READY` banner and drops to a plain shell instead.

### Backend

- `Agent.headless_chat` (computed_field, `models/agent.py`): `True` when (`agent_runtime == "cli-bridge"` and `harness == "omp"` and `omp_driver_for("omp") == "acp"` — ADR-084, harness property; the ADR-081 slug list is gone) or (`agent_runtime == "host"` and `harness == "hermes"` and `settings.hermes_driver == "acp"`). Exposed in `AgentRead`.
- `agent_chat_input._target_kind` gains `"acp-docker"` / `"acp-http"`; `send_text`, `send_keys` (`Escape` → `cancel`, everything else → `InputNotSupportedError`), `set_effort` (→ `config thinking=<level>`; `EffortSwitchRejectedError` on `ok:false`), and a new `set_model(agent, name)` (→ `config model=<name>`) route through `acp_chat_transport.py`.
- `send_text` asks the daemon for `mode: "queue"` (a mid-turn message is held, not refused) and turns an exact `/new` or `/clear` into `new_session` unless the agent advertises that command itself; an agent image without the op answers `unknown_op` (its old `acp_chat_ctl.py` rejects the op in argparse: exit 2, empty stdout, `invalid choice` — the Docker transport reads that as `unknown_op`, not as unreachable) → 409 `input_not_supported` (never a prompt the model would play along with). Refusals the daemon does not show as a card itself (`not_started` → 409 `agent_starting`, anything else → 409 `acp_refused`) are errors, never a silent 204. `clear_queue(agent)` → `queue_clear`, exposed as `POST /agents/{id}/chat/queue/clear` → `{"dropped": [text, …]}`.
- `effort_capabilities` → levels from `configOptions[id=thinking].options` in `acp-chat-state.json`; `slash_command_capabilities` → `commands`; `model_options_capabilities` → `configOptions[id=model].options` (label = name, command = `/model <value>`). Empty/missing state file → empty lists with reason `acp_state_missing` (never raises).
- `omp_chat.resolve_transcript_dir` accepts `host` + `harness == "hermes"` (dir `~/.mc/agents/hermes/omp-sessions`), so the Hermes daemon reuses the omp transcript format and the whole reader/preview stack unchanged.
- `_parse_custom_message`: `customType == "chat_error"` → `{"kind":"message","role":"teammate","source":{"kind":"error","title":"chat_error"},"error":{"code","detail"}}` so the frontend can style it.

### Frontend

- `Agent.headless_chat?: boolean` in `types.ts`.
- `ChatView`: when `agent.headless_chat` the Chat/Terminal toggle is not rendered and `effectiveView` is forced to `chat` unless `?view=terminal` is explicitly in the URL. Stop button sends `Escape` (unchanged API → cancel); for a headless agent it first calls `POST /agents/{id}/chat/queue/clear` and puts the held messages back into the composer, so the next held message does not start the moment the turn is cancelled. A mid-turn send shows as a queued row ("sent when the current reply ends") with take-back / edit, both backed by `queue/clear`.
- Error events (`source.kind === "error"`) render as a red-bordered system card with the code chip (Signal palette tokens, i18n keys `chat.error.<code>`).
- Effort chip, model picker and slash palette already come from capabilities; nothing hardcoded.

### Hermes (`scripts/hermes-bridge.py`, `HERMES_DRIVER=acp`)

- On start: import `acp_chat` from `$MC_REPO_PATH/docker/omp-bridge` and run `ChatSession` in a thread with `client_factory = ACPClient(command=["hermes","acp"], cwd=<hermes cwd>)`, `sessions_dir = ~/.mc/agents/hermes/omp-sessions/<encoded cwd>`, socket `~/.mc/agents/hermes/acp-chat.sock`.
- HTTP: `POST /chat/<op>` (JSON body) → socket → JSON response (status 200 on `ok`, 409 on `busy`, 502 unreachable).
- Task dispatch under `HERMES_DRIVER=acp`: `dispatch_poll_loop` sends the task prompt through the same `ChatSession.prompt` (so the chat shows the work) and waits for the turn to end; native TUI path remains the fallback when the driver is unset.

## Proofs (live, each PR before merge/deploy)

1. Message typed to the ACP agent appears as user line + streamed preview + final assistant line in the Sessions chat; `docker exec … acp_chat_ctl.py state` shows `turn` incremented.
2. `/usage` from the palette answers in the chat; `/model <other>` changes `configOptions.model.currentValue`.
3. Effort chip `high` → state `thinking=high`.
4. Stop during a long turn → turn ends with `cancelled`, no error card.
5. Sabotage: `config model=does-not-exist` → red `chat_error` card with `rpc_error`.
6. The ACP agent's Sessions page: no Chat/Terminal toggle; `?view=terminal` still opens the pane.
7. Hermes: chat message answered over `hermes acp`; history survives a bridge restart.

## Rollout

PR A (bridge daemon + ctl + entrypoint + tests) → PR B (backend) → PR C (frontend) → PR D (Hermes). Container recreate of the ACP agent only after Boss reports the #148 follow-up task finished; hermes-bridge reload only after Hermes' task G5 — both announced to Boss first.
