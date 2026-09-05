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

## `?mode=keys` — Tastendrücke mit Bestätigung (seit 05.09.2026)

Der Sessions-Chat schickt Boss-Nachrichten **nicht** mehr als rohe Bytes in
ein Pseudo-Terminal, sondern so:

```
ws://127.0.0.1:7682/?mode=keys[&session=…&socket=…]
→ {"type":"send_keys","keys":[{"literal":"text"},{"named":"Enter"}]}
← {"type":"ack","ok":true,"sent":2}            # oder {"ok":false,"error":"…","sent":N}
```

Die Bridge führt pro Eintrag `tmux -S <socket> send-keys -t <session> -l -- <text>`
bzw. `send-keys <Name>` aus (Namen: Escape, Enter, Up, Down, C-u — sonst
`ok:false`) und antwortet erst, wenn tmux zurück ist. Das Backend wertet nur
`ok:true` als zugestellt; alles andere wird `502 boss_delivery_failed`.

**Warum:** Der pty-Weg spawnte pro Nachricht `tmux attach` in ein frisches pty,
schrieb sofort hinein und beendete den Client direkt nach dem letzten Byte.
Bytes im Attach-Handshake oder kurz vor dem Kill gingen verloren — je nach
Last fehlte das Enter (Text sass unabgeschickt im Eingabefeld) oder der ganze
Text, während das Log „wrote 57 bytes" meldete. Der pty-Modus bleibt für das
Browser-Terminal (Sessions-Seite) bestehen.

Tests: `python3 -m pytest docker/host-pty-bridge/tests -q`
