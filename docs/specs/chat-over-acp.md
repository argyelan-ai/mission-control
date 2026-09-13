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

- Multi-turn concurrency. One turn at a time per agent chat session; a second prompt while busy is rejected with `busy` (the composer already disables send while working).

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
transcript *.jsonl  ·  previews/*.jsonl  ·  acp-chat-state.json
   ▲
   │ writes
acp_chat.py --serve   (long-lived chat daemon; one `omp acp` / `hermes acp` child, one ACP session, session/load on restart)
   ▲ Unix socket  $OMP_HOME/acp-chat.sock  (JSON lines request/response)
acp_chat_ctl.py <op>  (CLI shim used by docker exec)      ·      hermes-bridge.py /chat/<op> (HTTP shim on the host)
```

### Component: `docker/omp-bridge/acp_chat.py` (chat daemon)

- `ChatSession(client_factory, cwd, state_dir, sessions_dir)` — pure-Python core, testable with a fake ACP process (`tests/fake_acp_server.py`) or an in-memory fake client.
- Startup: read `state_dir/acp-chat.json` (`{"sessionId", "transcript"}`); `initialize`; try `session/load` with the stored id → on success append to the stored transcript file (history survives restarts); on failure `session/new`, new transcript file, and emit `chat_error{code:"session_reset"}`.
- Uses `acp_chat_events.ACPEventMapper` + `ChatEventSink` + `PreviewEventSink` exactly like `run_acp_once` (user line via `map_user_prompt`, streaming previews, ONE final assistant line with usage).
- `configOptions` (from `session/new`/`session/load` result and `config_option_update` notifications) and `available_commands_update` are kept in memory and mirrored to `sessions_dir/acp-chat-state.json` after every change.
- Permissions: `OMP_ACP_PERMISSIONS` policy (yolo → allow), same helper as the bridge.
- Errors → transcript `custom_message` line, `customType: "chat_error"`, content = human text, plus `data: {"code": ..., "detail": ...}`. Codes: `rpc_error` (JSON-RPC error on prompt/config), `provider_error` (`401`/`403`/`429`/quota text detected in the error or in the final agent text when the turn produced no text), `empty_turn` (turn ended with stopReason `end_turn` and zero agent text), `process_exit` (child died; daemon restarts it and re-loads the session), `busy`, `session_reset`, `cancelled` is NOT an error (it produces a normal end-of-turn).
- Socket server: `$OMP_HOME/acp-chat.sock` (path configurable via `--socket`), newline-delimited JSON; each connection = one request/one response.

### Control protocol (socket and HTTP share the schema)

| op | request | response |
|----|---------|----------|
| `prompt` | `{"op":"prompt","text":"..."}` | `{"ok":true,"turn":n}` immediately (turn runs async) · `{"ok":false,"error":"busy"}` |
| `cancel` | `{"op":"cancel"}` | `{"ok":true}` (no-op when idle) |
| `config` | `{"op":"config","id":"thinking","value":"high"}` | `{"ok":true,"configOptions":[...]}` / `{"ok":false,"error":"rpc_error","detail":"..."}` |
| `state` | `{"op":"state"}` | `{"ok":true, ...state}` |

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
  "updatedAt": "2026-09-13T12:00:00Z",
  "lastError": null | {"code":"…","detail":"…","at":"…"}
}
```

Slash commands are sent as prompt text (`/usage`, `/model X`); omp executes them inside `session/prompt`. `/model` is additionally offered via `config id=model` so the composer's model picker uses the option list.

### CLI shim `acp_chat_ctl.py`

`python3 /opt/omp-bridge/acp_chat_ctl.py <op> [--json '{...}']` → prints the daemon's JSON response, exit 0 when `ok`, 2 when `ok:false`, 3 when the socket is unreachable (`{"ok":false,"error":"unreachable"}`).

### entrypoint (omp image)

When `OMP_DRIVER=acp`, `start_native` additionally opens tmux window 3 `win3`: `exec python3 /opt/omp-bridge/acp_chat.py --serve`. Window 0 still exists but no longer runs the native TUI (fix omp-acp-no-tui-window, 13.09.2026) — it prints an `OMP_ACP_READY` banner and drops to a plain shell instead.

### Backend

- `Agent.headless_chat` (computed_field, `models/agent.py`): `True` when (`agent_runtime == "cli-bridge"` and `harness == "omp"` and `slug in settings.omp_acp_agents()`) or (`agent_runtime == "host"` and `harness == "hermes"` and `settings.hermes_driver == "acp"`). Exposed in `AgentRead`.
- `agent_chat_input._target_kind` gains `"acp-docker"` / `"acp-http"`; `send_text`, `send_keys` (`Escape` → `cancel`, everything else → `InputNotSupportedError`), `set_effort` (→ `config thinking=<level>`; `EffortSwitchRejectedError` on `ok:false`), and a new `set_model(agent, name)` (→ `config model=<name>`) route through `acp_chat_transport.py`.
- `effort_capabilities` → levels from `configOptions[id=thinking].options` in `acp-chat-state.json`; `slash_command_capabilities` → `commands`; `model_options_capabilities` → `configOptions[id=model].options` (label = name, command = `/model <value>`). Empty/missing state file → empty lists with reason `acp_state_missing` (never raises).
- `omp_chat.resolve_transcript_dir` accepts `host` + `harness == "hermes"` (dir `~/.mc/agents/hermes/omp-sessions`), so the Hermes daemon reuses the omp transcript format and the whole reader/preview stack unchanged.
- `_parse_custom_message`: `customType == "chat_error"` → `{"kind":"message","role":"teammate","source":{"kind":"error","title":"chat_error"},"error":{"code","detail"}}` so the frontend can style it.

### Frontend

- `Agent.headless_chat?: boolean` in `types.ts`.
- `ChatView`: when `agent.headless_chat` the Chat/Terminal toggle is not rendered and `effectiveView` is forced to `chat` unless `?view=terminal` is explicitly in the URL. Stop button sends `Escape` (unchanged API → cancel).
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
