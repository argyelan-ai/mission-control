# Boss Host-Agent Setup

Boss läuft als macOS launchd-Job auf dem Host (nicht im Docker-Container).
Echter `claude` Binary mit dem OAuth-Login des Operators + Opus 4.7.

## Architektur

```
Browser xterm.js
  ↓ WS /api/v1/host-agents/{id}/terminal
Backend (FastAPI WS Proxy)
  ↓ WS to ws://host.docker.internal:7681
ttyd (com.openclaw.boss-ttyd, port 7681)
  ↓ pty
tmux -S ~/.mc/agents/boss-host/.tmux.sock attach -t boss-host
  ├─ Window 0: claude (Opus 4.7)
  └─ Window 1: poll.sh → POST localhost:8000/api/v1/agent/me/poll
```

## Install

```bash
# 1. Voraussetzungen
brew install ttyd
which claude   # $HOME/.local/bin/claude — official Anthropic Claude Code

# 2. agent-Verzeichnis anlegen
mkdir -p ~/.mc/agents/boss-host/{claude-config,logs}

# 3. SOUL.md + andere config-Dateien aus Container-Boss kopieren (oder via Backend bootstrap)
cp ~/.mc/agents/boss/claude-config/{SOUL,TOOLS,USER,HEARTBEAT,MEMORY}.md \
   ~/.mc/agents/boss-host/claude-config/

# 4. Token via Backend Bootstrap holen
TOKEN=$(curl -sf "http://localhost:8000/api/v1/internal/bootstrap?agent_name=boss" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['MC_AGENT_TOKEN'])")
cat > ~/.mc/agents/boss-host/agent.env <<EOF
MC_API_URL=http://localhost:8000
MC_TOKEN=$TOKEN
MC_AGENT_TOKEN=$TOKEN
AGENT_NAME=boss
POLL_INTERVAL=10
EOF
chmod 600 ~/.mc/agents/boss-host/agent.env

# 5. Scripts in den Runtime-Pfad kopieren (Repo → Runtime)
cp docker/boss-host/{entrypoint,start-claude,poll}.sh ~/.mc/agents/boss-host/
chmod +x ~/.mc/agents/boss-host/*.sh

# 6. Launchd plists
cp docker/boss-host/com.openclaw.boss.plist ~/Library/LaunchAgents/
cp docker/boss-host/com.openclaw.boss-ttyd.plist ~/Library/LaunchAgents/

# 7. Aktivieren
launchctl load -w ~/Library/LaunchAgents/com.openclaw.boss.plist
launchctl load -w ~/Library/LaunchAgents/com.openclaw.boss-ttyd.plist
```

## Status pruefen

```bash
launchctl list | grep openclaw.boss
tmux -S ~/.mc/agents/boss-host/.tmux.sock list-sessions
tail ~/.mc/agents/boss-host/logs/poll.log
curl -sI http://127.0.0.1:7681/   # ttyd
```

## Stoppen / Rollback

```bash
launchctl unload ~/Library/LaunchAgents/com.openclaw.boss-ttyd.plist
launchctl unload ~/Library/LaunchAgents/com.openclaw.boss.plist
# Falls Container-Boss wieder rein soll: in docker/docker-compose.agents.yml
# den mc-agent-boss Block wieder einkommentieren + docker compose up -d.
```
