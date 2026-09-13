# Hermes Worker — Host-Side Artifacts (Phase 24)

Reference copies of host-side files for the Hermes Worker integration. The
authoritative install paths are outside the repo; these copies live here for
git traceability and onboarding.

## Files

| Repo path | Install path | Mode |
|-----------|--------------|------|
| `entrypoint.sh` | `~/.mc/agents/hermes/entrypoint.sh` | 755 |
| `com.mc.hermes-bridge.plist` | `~/Library/LaunchAgents/com.mc.hermes-bridge.plist` | 644 |

The bridge script itself lives at `scripts/hermes-bridge.py` (in the repo,
launched directly by launchd via the plist's `ProgramArguments`).

## Install (manual, for now)

Plan 24-08 will add a `POST /agents/{id}/provision-hermes` endpoint that
copies these to the host paths and calls `launchctl bootstrap`. Until then:

```bash
mkdir -p ~/.mc/agents/hermes/logs
cp docker/hermes/entrypoint.sh ~/.mc/agents/hermes/entrypoint.sh
chmod 755 ~/.mc/agents/hermes/entrypoint.sh
cp docker/hermes/com.mc.hermes-bridge.plist ~/Library/LaunchAgents/com.mc.hermes-bridge.plist
chmod 644 ~/Library/LaunchAgents/com.mc.hermes-bridge.plist

# Provision agent.env first (plan 24-08), THEN load the launchd job:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mc.hermes-bridge.plist
```

## Verify

```bash
launchctl list | grep com.mc.hermes-bridge
curl -sS http://127.0.0.1:18794/health
tmux attach -t hermes-worker
```

## `HERMES_DRIVER=acp` — chat over ACP (optional)

With `HERMES_DRIVER=acp` the bridge does **not** start the tmux TUI. Instead it
holds one long-lived `hermes acp --accept-hooks` session through the shared
chat daemon (`docker/omp-bridge/acp_chat.py`) and serves it over HTTP:

| | native (default) | `HERMES_DRIVER=acp` |
|---|---|---|
| Interface | tmux `hermes-worker` TUI | Sessions chat only (`headless_chat`) |
| Dispatch | `tmux send-keys` | the same ACP session, one task per turn |
| Backend control | — | `POST /chat/{prompt,cancel,config,state}` |

- Transcript + `acp-chat-state.json`: `~/.mc/agents/hermes/omp-sessions/<encoded-cwd>/`
  — exactly where the backend's omp chat reader looks.
- ACP working directory: `~/.mc/workspaces/hermes` (override `HERMES_ACP_CWD`).
- `POST /start` · `/restart` · `/stop` act on the daemon; the ACP session
  itself survives a restart (`session/load`), so the chat history does too.
- `GET /health` reports `driver` and `chat_daemon_running`.

Set the variable in the plist's `EnvironmentVariables` (see below) and reload
the job. Leave it out and every path stays byte-identical to the native one.

```bash
launchctl kickstart -k gui/$(id -u)/com.mc.hermes-bridge
curl -sS http://127.0.0.1:18794/health          # driver, chat_daemon_running
curl -sS -X POST http://127.0.0.1:18794/chat/state
```

## Architecture

```
launchd (com.mc.hermes-bridge)
  └─> python3 scripts/hermes-bridge.py (HTTP :18794, 127.0.0.1)
        └─> POST /start triggers tmux new-session "hermes-worker"
              └─> entrypoint.sh
                    ├─ sources ~/.mc/agents/hermes/agent.env
                    └─ while true; do hermes; sleep 5; done
```

## Security

- Bridge binds **127.0.0.1 only** (Phase 24 L-C decision). Even on shared
  Tailscale Macs the `/start` endpoint is unreachable from peers.
- `agent.env` will be `chmod 600` (rendered in plan 24-08); contains
  `MC_AGENT_TOKEN`.
- No env-vars are echoed to logs (verify after first run).
