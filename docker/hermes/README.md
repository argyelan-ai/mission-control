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
