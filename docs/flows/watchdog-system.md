# Watchdog System

> Periodische Systemueberwachung: Agent-Health, Sessions, Token-Sync, Phase-Completion, Queue-Processing.

## Uebersicht

```
30s-Loop → Agent Health → Sessions Check → Token Sync → System Health → Metrics
                                                      → Phase Completion → Queue Processing
                                                      → Expired Approvals
```

**Singleton:** `services/watchdog.py` → `watchdog = WatchdogService()`
**Startup:** via FastAPI Lifespan (`main.py`)
**Lock:** Redis Lock `mc:watchdog:lock` (nur ein Worker)

## Loop-Ablauf

```
_run():
  → Grace Period: 10s nach Startup warten
  → Endlos-Loop (alle 30s):
      → Redis Lock acquiren (mc:watchdog:lock)
      → DB Session oeffnen
      → 1. _check_agent_health()
      → 2. _check_expired_approvals()
      → 3. Gateway Sessions laden (rpc.sessions_list)
      → 4. _check_agent_sessions(sessions)
      → 5. _sync_agent_tokens(sessions)
      → 6. db_latency, redis_latency = _check_system_health()
      → 7. _collect_system_metrics(db_latency, redis_latency)
      → 8. _check_phase_completions()
      → 9. _process_task_queues()
      → checks_total++, last_check_at = now()
```

## 1. Agent Health Check

```
_check_agent_health(session):
  → Alle Agents mit status="restarting" laden
  → Seit wann restarting? > AGENT_RESTART_TIMEOUT_MINUTES (3min)?
      → JA: status = "error"
      → emit_event("agent.restart_timeout", severity="warning")
```

## 2. Expired Approvals

```
_check_expired_approvals(session):
  → Alle Approvals mit status="pending" + expires_at < now()
  → Status → "expired"
  → emit_event("approval.expired")
```

## 3. RPC Connection Check

```
_check_rpc_connection():
  → Via Callback: rpc.on_state_change(_on_rpc_state_change)
  → Bei Disconnect: emit_event("gateway.disconnected", severity="warning")
  → Bei Reconnect: emit_event("gateway.connected")
  → State-Flags verhindern Duplikat-Events
```

## 4. Agent Sessions Check

```
_check_agent_sessions(session, gw_sessions):
  → Alle Agents mit gateway_agent_id laden
  → Gateway-Sessions als Dict: {agent_id: session_data}

  → Restarting-Agents:
      → Hat wieder eine Session? → status = "online", last_seen_at = now()
      → emit_event("agent.online")

  → Online-Agents:
      → Keine aktive Session mehr? → status = "offline"
      → emit_event("agent.offline")
      → current_task_id = None (Task nicht mehr aktiv)
```

## 5. Token Sync

```
_sync_agent_tokens(session, gw_sessions):
  → Pro Agent mit Gateway-Session:
      → context_tokens aus Session-Daten lesen
      → session_message_count aus Session-Daten
      → total_compactions aus Session-Daten (falls vorhanden)
      → Agent UPDATE in DB
```

## 6. System Health

```
_check_system_health(session):
  → DB: SELECT 1 (Latenz messen)
  → Redis: PING (Latenz messen)
  → Latenz > 2000ms? → emit_event("system.slow_*", severity="warning")
  → Return (db_latency_ms, redis_latency_ms)
```

## 7. System Metrics

```
_collect_system_metrics(db_latency, redis_latency):
  → psutil: CPU%, RAM%, Disk%
  → Redis SET mc:system:metrics:current (TTL 120s)
  → Redis LPUSH mc:system:metrics:history (max 60 Eintraege)
  → Daten-Format:
      {
        "cpu_percent": 23.5,
        "memory_percent": 45.2,
        "disk_percent": 67.8,
        "db_latency_ms": 1.2,
        "redis_latency_ms": 0.5,
        "timestamp": "2026-02-24T..."
      }
```

## 8. Phase Completion

```
_check_phase_completions(session):
  → Parent-Tasks laden: status="in_progress" + hat Subtasks
  → Pro Parent:
      → Alle Subtasks laden
      → Alle done?
          → JA: Parent.status = "review"
          → Board Lead finden (is_board_lead=True + gateway_agent_id)
          → rpc.chat_send(lead, "Phase {title} abgeschlossen, alle Subtasks done")
          → emit_event("task.phase_completed")
```

## 9. Queue Processing

```
_process_task_queues(session):
  → Agents laden: gateway_agent_id gesetzt
  → Agent frei? (keine in_progress UND keine inbox+dispatched_at Task)
  → Pro freier Agent:
      → task_queue.dequeue_task(agent_id)
      → Task gefunden?
          → Task aus DB laden (noch inbox?)
          → Task.dispatched_at = now() (Status bleibt inbox — wartet auf Agent-ACK!)
          → _build_dispatch_message(session, task) — aus dispatch.py
          → rpc.chat_send(gateway_agent_id, message)
          → emit_event("task.dispatched")
```

## Redis Keys

| Key | Typ | TTL | Beschreibung |
|-----|-----|-----|-------------|
| `mc:watchdog:lock` | String | 60s | Watchdog Deduplizierung |
| `mc:system:metrics:current` | String (JSON) | 120s | Aktuelle Metriken |
| `mc:system:metrics:history` | List (JSON) | - | Letzte 60 Snapshots |
| `mc:agent:{id}:task_queue` | List | - | Task FIFO Queue |

## Wer konsumiert Watchdog-Daten?

| Consumer | Endpoint | Daten |
|----------|----------|-------|
| Frontend Dashboard | `GET /api/v1/system/status` | Watchdog running, last_check, checks_total |
| Frontend Metrics | `GET /api/v1/system/metrics` | Task/Agent/Approval Counts |
| Frontend Sparklines | `GET /api/v1/system/metrics/history` | 60 Metriken-Snapshots |
| Agent Cards | Agent.context_tokens, session_message_count | via Token-Sync |

## Side-Effects

| Check | DB | Redis | SSE | Discord | RPC |
|-------|----|----|-----|---------|-----|
| Agent Health | Agent UPDATE | broadcast | agent.status | Bei warning+ | - |
| Expired Approvals | Approval UPDATE | broadcast | approval.expired | - | - |
| RPC State | - | broadcast | gateway.status | Bei warning+ | connect() |
| Agent Sessions | Agent UPDATE | broadcast | agent.online/offline | - | sessions_list() |
| Token Sync | Agent UPDATE | - | - | - | sessions_list() |
| System Health | - | metrics SET | - | Bei slow | - |
| Phase Completion | Task UPDATE | broadcast | task.updated | - | chat_send() |
| Queue Processing | Task + Agent UPDATE | dequeue | task.dispatched | - | chat_send() |

## 10. Pending Dispatches

```
_process_pending_dispatches(session, gw_sessions):
  → Agents mit gateway_agent_id + pending_dispatch Queue nicht leer
  → Agent hat jetzt eine Session? (aus gw_sessions)
  → Max 3 Tasks pro Zyklus:
      → Task aus DB laden (status=inbox + assigned_agent_id gesetzt?)
      → chat_send() mit Dispatch-Message
      → Erfolg: Task.dispatched_at = now()
      → Fehlgeschlagen: zurueck in Queue
```

## Edge Cases

- **Grace Period**: 10s nach Startup — verhindert False Positives bei langsamen Starts
- **Redis Lock**: Nur ein Watchdog-Worker zur gleichen Zeit (Multi-Container safe)
- **State Flags**: `_rpc_was_connected` verhindert Duplikat-Events bei wiederholten Checks
- **Offline ohne Queue-Clear**: Wenn Agent offline geht, bleibt seine Queue erhalten
- **Sessions ohne Agent**: Gateway-Sessions die keinem MC-Agent zugeordnet sind, werden ignoriert
- **Agent-Busy Definition**: in_progress ODER inbox+dispatched_at — dispatched-but-not-ACK'd zaehlt als busy
