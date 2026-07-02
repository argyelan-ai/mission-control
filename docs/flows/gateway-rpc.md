# Gateway RPC (WebSocket)

> WebSocket-basierte Kommunikation zwischen Mission Control und dem OpenClaw Gateway.

## Uebersicht

```
Mission Control ←WebSocket→ OpenClaw Gateway (Hetzner via Tailscale)
                   ↕
              Ed25519 Auth
              JSON-RPC 2.0
              Auto-Reconnect
```

**Singleton:** `services/openclaw_rpc.py` → `rpc = OpenClawRPC()`

## 1. WebSocket-Setup & Handshake

```
rpc.connect():
  → WebSocket zu Gateway WS-URL oeffnen
  → Device Identity laden:
      ~/.openclaw/identity/device.json (device_id, device_name)
      ~/.openclaw/identity/device-auth.json (private_key Ed25519)
  → Handshake-Sequenz:
      1. Gateway sendet Challenge Event: {"method": "connect", "params": {"challenge": "..."}}
      2. MC signiert Challenge mit Ed25519 Private Key
      3. MC sendet Connect Request: {"method": "connect", "params": {
           "deviceId": "...",
           "signature": "...",  // Ed25519 Signatur
           "version": "v2"
         }}
      4. Gateway sendet Connect Response: {"result": {"authenticated": true}}
  → connected = True
  → _receive_loop() starten (Background-Task)
  → State-Change Callbacks ausfuehren
```

## 2. Ed25519 Device Auth

```python
# Signatur-Erstellung (v2):
payload = json.dumps({"challenge": challenge, "deviceId": device_id})
signature = private_key.sign(payload.encode())
# v1 Fallback: nur Challenge signieren (ohne deviceId)
```

**Dateien:**
- `~/.openclaw/identity/device.json` — device_id, device_name
- `~/.openclaw/identity/device-auth.json` — Ed25519 Private Key (PEM)

## 3. Request/Response Pattern

```
rpc.request(method, params, timeout=30):
  → request_id = Inkrementierender Counter
  → Future erstellen → self._pending[request_id] = future
  → JSON senden: {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
  → await future (mit Timeout)
  → Response: {"jsonrpc": "2.0", "id": request_id, "result": ...}
  → Oder Error: {"jsonrpc": "2.0", "id": request_id, "error": {"code": ..., "message": ...}}
```

## 4. Receive-Loop

```
_receive_loop():
  → Endlos-Loop: WebSocket Messages lesen
  → JSON parsen
  → IF "id" in message UND id in _pending:
      → Future resolven mit result/error
  → IF "method" in message (Server-Push Event):
      → Event-Handler (z.B. State Changes)
  → Bei WebSocket-Error/Close:
      → connected = False
      → State-Change Callbacks (connected=False)
      → Auto-Reconnect versuchen
```

## 5. Auto-Reconnect

```
Bei Verbindungsverlust:
  → connected = False
  → Callbacks: on_state_change(connected=False)
  → Watchdog erkennt RPC-Disconnect (_on_rpc_state_change)
  → emit_event("gateway.disconnected", severity="warning")
  → Naechster Watchdog-Zyklus (30s): rpc.connect() versuchen
  → Bei Erfolg: emit_event("gateway.connected")
```

## 6. Verfuegbare RPC-Methoden

### Core
| Methode | RPC Method | Timeout | Beschreibung |
|---------|-----------|---------|-------------|
| `health()` | `health` | 10s | Gateway Health Check |
| `agents_list()` | `agents.list` | 15s | Alle Agents |
| `agents_get(id)` | `agents.get` | 10s | Agent Details |
| `agents_update(id, **kw)` | `agents.update` | 15s | Agent updaten |
| `models_list()` | `models.list` | 15s | Verfuegbare AI Models |

### Chat & Sessions
| Methode | RPC Method | Timeout | Beschreibung |
|---------|-----------|---------|-------------|
| `chat_send(agent_id, msg)` | `chat.send` | 60s | Nachricht an Agent (findet Session) |
| `chat_history(key, limit)` | `chat.history` | 15s | Chat-History einer Session |
| `sessions_list(limit)` | `sessions.list` | 15s | Aktive Sessions |
| `sessions_reset(key)` | `sessions.reset` | 15s | Session zuruecksetzen |
| `sessions_history(key, limit)` | `sessions.history` | 15s | Session-History |

### Config
| Methode | RPC Method | Timeout | Beschreibung |
|---------|-----------|---------|-------------|
| `config_get(keys)` | `config.get` | 10s | Gateway Config lesen |
| `config_patch(patch)` | `config.patch` | 15s | Config aendern (Optimistic Concurrency) |

### Files
| Methode | RPC Method | Timeout | Beschreibung |
|---------|-----------|---------|-------------|
| `agents_files_get(id, type)` | `agents.files.get` | 10s | Agent-Datei lesen |
| `agents_files_set(id, type, content)` | `agents.files.set` | 15s | Agent-Datei schreiben |

### Skills
| Methode | RPC Method | Timeout | Beschreibung |
|---------|-----------|---------|-------------|
| `skills_status(agent_id)` | `skills.status` | 15s | Skill-Status eines Agents |
| `skills_bins()` | `skills.bins` | 15s | Verfuegbare Skill-Binaries |
| `skills_install(name, id, timeout)` | `skills.install` | 120s | Skill installieren |
| `skills_update(key, enabled, ...)` | `skills.update` | 15s | Skill konfigurieren |

### Polling
```
poll_agent_reply(gateway_agent_id, max_attempts=15, interval=2):
  → Loop: chat_history laden
  → Letzte Message = assistant?
      → JA: Text-Parts extrahieren (thinking ignorieren)
      → OpenClaw-interne Tags entfernen (<oc-...>)
      → Return cleaned text
  → max_attempts erreicht? → Return None
```

## Session-Key Pattern

```
Session-Keys im Gateway:
  "agent:{gateway_agent_id}:main"    — Hauptsession
  "agent:{gateway_agent_id}"         — Fallback-Pattern

chat_send() sucht aktive Session:
  → sessions_list() laden
  → Match: session.agentId == gateway_agent_id
  → Kein Match? → RPCError("No active session")
```

## Wer nutzt RPC?

| Caller | Methoden | Zweck |
|--------|----------|-------|
| `dispatch.py` | `chat_send()` | Task-Nachrichten an Agents |
| `watchdog.py` | `sessions_list()`, `chat_send()` | Health Checks, Phase-Notifications, Queue Processing |
| `task_runner.py` | `chat_send()` | Task-Reminders und Eskalation |
| `gateway.py` (Router) | Alle | Frontend-Proxy zu Gateway |
| `planner.py` | `chat_send()`, `poll_agent_reply()` | Planner-Chat |
| `research.py` | `chat_send()`, `poll_agent_reply()` | Research-Chat |
| `chat.py` | `chat_send()`, `poll_agent_reply()`, `sessions_history()` | Agent DMs |
| `agents.py` | `agents_files_set()`, `agents_update()` | Provisioning + Config Sync |
| `skills.py` | `skills_*()` | Skill Management |
| `models.py` | `models_list()` | Model-Katalog |
| `gateway_sync.py` | `agents_list()`, `config_get()` | Agent-Sync |

## Edge Cases

- **v1/v2 Auth**: Beide Signatur-Versionen werden versucht (v2 zuerst, v1 Fallback)
- **Timeout Handling**: Jede Methode hat eigenen Timeout, pending Futures werden aufgeraeumt
- **Idempotency**: `chat_send()` generiert UUID als Idempotency-Key
- **Connection State**: `connected` Property, Callbacks via `on_state_change()`
- **Concurrent Requests**: Mehrere Requests gleichzeitig moeglich (Request ID Mapping)
