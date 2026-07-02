# Host PTY Bridge

Mini WebSocket-Server (Port 7682, 127.0.0.1) der direkt an die Boss-Host
tmux-Session attached. Pendant zum docker-exec-PTY-Pattern bei
Container-Agents. Ersetzt ttyd, weil ttyd's Frame-Protokoll (Command-Byte
Prefix) Sonder-Wrapping im Backend brauchte.

## Architektur

```
Backend (Docker)
  ↓ ws://host.docker.internal:7682/
host-pty-bridge.py
  ↓ pty.openpty() + tmux attach
tmux -S ~/.mc/agents/boss-host/.tmux.sock boss-host:0
```

Wire-Format: rohe Bytes in beide Richtungen. Resize via JSON
`{"type":"resize","cols":N,"rows":N}` (matcht das useAgentTerminal Hook
Format vom Frontend).

## Install

```bash
# Voraussetzung
python3 -c "import websockets" || pip3 install --user websockets

# Plist installieren + starten
cp docker/host-pty-bridge/com.openclaw.host-pty-bridge.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.openclaw.host-pty-bridge.plist

# Verify
launchctl list | grep openclaw.host-pty-bridge
lsof -iTCP:7682 -sTCP:LISTEN
```

## Stoppen

```bash
launchctl unload ~/Library/LaunchAgents/com.openclaw.host-pty-bridge.plist
```

## Logs

`~/.mc/agents/boss-host/logs/host-pty-bridge.{out,err}`
