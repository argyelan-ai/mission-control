# Dispatch System — Unified PUSH + ACK Handshake

> Alle Agents bekommen Tasks via PUSH (chat_send). Kein PULL-Modus mehr.
> Agents muessen Tasks explizit bestaetigen (ACK) — ohne ACK wird automatisch eskaliert.

## Uebersicht

```
Task erstellt → auto_dispatch_task()
  → Pre-assigned? → direkt an zugewiesenen Agent pushen
  → Board Lead zuerst (Orchestrator)
  → Fallback: erster Agent mit Gateway
  → Kein Agent → Warning-Event (manuelle Zuweisung noetig)
  → Agent busy? → Redis Queue (FIFO)
  → Agent frei? → Push:
      1. chat_send() (bestehende Session)
      2. Fehlgeschlagen? → pending_dispatch Queue (Watchdog-Nachlieferung)
  → Task bleibt "inbox" — Agent muss ACK senden (PATCH status: in_progress)
  → Kein ACK nach 10min → Re-Assign + Eskalation
```

**Hauptdatei:** `services/dispatch.py`
**Queue:** `services/task_queue.py`
**Watchdog-Integration:** `services/watchdog.py` (`_process_task_queues()` + `_process_pending_dispatches()` + `_check_undispatched_tasks()`)

## 1. Dispatch-Trigger

Auto-Dispatch wird ausgeloest von:

| Trigger | Datei | Bedingung |
|---------|-------|-----------|
| Task erstellt (User) | `routers/tasks.py` | Board.auto_dispatch_enabled + kein assigned_agent_id |
| Task erstellt (Agent) | `routers/agent_scoped.py` | Board.auto_dispatch_enabled |
| Plan finalisiert (Phase 1) | `routers/planner.py` | Nur Phase-1-Subtasks |
| Phase gestartet | `routers/tasks.py` | Parent → in_progress: alle inbox-Subtasks |
| Queue Processing | `services/watchdog.py` | Agent wird frei (keine aktive in_progress Task) |
| Pending Delivery | `services/watchdog.py` | Agent bekommt Session (war vorher offline) |
| Undispatched Recovery | `services/watchdog.py` | Task assigned + aktiv aber nie dispatcht |

## 2. Target-Findung

```
auto_dispatch_task(task_id, board_id):
  → Task.assigned_agent_id gesetzt?
      → JA: Agent laden, direkt an ihn pushen (kein find_dispatch_target)
      → NEIN: find_dispatch_target()

find_dispatch_target(session, task, board_id) → Agent | None:
  → 1. BOARD LEAD (Prioritaet!)
      → Agent mit is_board_lead=True + gateway_agent_id
      → Return board_lead

  → 2. FALLBACK (kein Board Lead mit Gateway)
      → Erster Agent mit gateway_agent_id
      → Warning-Event: "Board Lead offline"
      → Return fallback_agent

  → 3. KEIN AGENT
      → Return None
      → Warning-Event: "Kein Agent verfuegbar"
```

## 3. Unified PUSH Dispatch + ACK Handshake

```
auto_dispatch_task(task_id, board_id):  [BackgroundTask]
  → Board laden: auto_dispatch_enabled?
  → Task laden

  → Pre-assigned? (assigned_agent_id != None)
      → JA: Agent laden, direkt pushen
      → NEIN: find_dispatch_target() + zuweisen

  → Agent busy? (in_progress ODER inbox+dispatched_at = dispatched aber nicht ACK'd)
      → JA: task_queue.enqueue_task(agent_id, task_id)
      → NEIN: weiter

  → Task.status BLEIBT "inbox" (kein sofortiger Status-Wechsel!)
  → Push:
      1. chat_send(gateway_agent_id, message)
         → Erfolg: Task.dispatched_at = now()    → mode="push"
      2. Fehlgeschlagen:
         → enqueue_pending_dispatch(agent_id, task_id) → mode="push_pending"
  → emit_event("task.auto_dispatched", mode=...)

ACK-Handshake:
  → Agent empfaengt Dispatch-Message mit ACK-Instruktion
  → Agent muss PATCH /agent/boards/{board_id}/tasks/{task_id} → {"status": "in_progress"}
  → Backend setzt: task.status = "in_progress", task.ack_at = now(), task.started_at = now()
  → Kein ACK nach 10min → Task Runner eskaliert (siehe Abschnitt 7)
```

### Dispatch State Machine
```
inbox (erstellt)
  → inbox + assigned_agent_id (zugewiesen)
  → inbox + dispatched_at (Nachricht gesendet, wartet auf ACK)
  → in_progress + ack_at (Agent hat bestaetigt — arbeitet)
  → review | blocked | failed | done (Endstatus)
```

### Neue Task-Felder (Migration 0018)
| Feld | Typ | Bedeutung |
|------|-----|-----------|
| `dispatched_at` | DateTime nullable | Wann chat_send() erfolgreich war |
| `ack_at` | DateTime nullable | Wann Agent PATCH status: in_progress gesendet hat |

## 4. Dispatch-Message

```
_build_dispatch_message(task, agent, session):
  → Basis: "# Neue Aufgabe: {task.title}" + Priority + Task-ID + Board-ID
  → Beschreibung: task.description
  → Projekt-Kontext (wenn task.project_id gesetzt)
  → Board Memory Kontext (bis 3 gepinnte/neueste Eintraege)
  → Agent-Lessons (was dieser Agent gelernt hat)
  → Relevante Lessons (Keyword-Match)
  → Team-Erkenntnisse
  → Intelligence Insights
  → ACK-Instruktion (NEU):
      → "Bestaetige diesen Task SOFORT: PATCH status: in_progress"
      → "Ohne Bestaetigung wird der Task nach 10 Minuten automatisch neu zugewiesen"
  → Kommentar-Protokoll (Update/Evidence/Next Format)
  → Callback-Protokoll:
      → PATCH .../tasks/{id} → status: review | blocked | failed
      → POST .../tasks/{id}/comments → Kommentar
```

## 5. Queue-Management

### Task Queue (Agent busy)
```
Redis Key: mc:agent:{agent_id}:task_queue (FIFO, RPUSH/LPOP)
Enqueue: task_queue.enqueue_task(agent_id, task_id)
Dequeue: task_queue.dequeue_task(agent_id) — via Watchdog
```

### Pending Dispatch Queue (Agent offline)
```
Redis Key: mc:agent:{agent_id}:pending_dispatch (FIFO, RPUSH/LPOP)
Enqueue: task_queue.enqueue_pending_dispatch(agent_id, task_id)
Dequeue: task_queue.dequeue_pending_dispatch(agent_id) — via Watchdog
```

### Watchdog Processing (alle 30s)
```
_process_task_queues():
  → Alle Agents mit gateway_agent_id (unified, nicht nur Board Lead)
  → Agent frei? (keine in_progress UND keine inbox+dispatched_at Task) → dequeue + dispatch
  → Bei Erfolg: Task.dispatched_at = now() (NICHT status = in_progress!)

_process_pending_dispatches(gw_sessions):
  → Alle Agents mit gateway_agent_id
  → Pending Queue nicht leer?
  → Agent hat jetzt eine Session? (aus gw_sessions)
  → Max 3 Tasks pro Zyklus nachholen via chat_send()
  → Bei Erfolg: Task.dispatched_at = now()
  → Fehlgeschlagen? → zurueck in Queue, naechster Zyklus

_check_undispatched_tasks():
  → Tasks mit: assigned_agent_id != NULL + dispatched_at = NULL + status IN (in_progress, review)
  → SEQUENTIELL: Max 1 Task pro Agent pro Zyklus (kein Batch-Overload)
  → BUSY-CHECK: Hat Agent schon dispatched in_progress Task? → Skip
  → AUTO-ACK: Task schon in_progress? → ack_at = now() (kein sinnloser ACK noetig)
  → Bei Erfolg: dispatched_at = now(), Event: task.undispatched_recovery
  → Faengt Tasks auf die durch manuelle Reassignment oder unterbrochenen Dispatch nie gesendet wurden
```

## 6. Review-Handoff (Push)

```
Developer setzt status=review:
  → Reviewer finden (_find_reviewer)
  → Task zuweisen
  → Push: Review-Nachricht an Reviewer via chat_send()
  → Fallback: pending_dispatch Queue

Reviewer setzt status=in_progress (Reject):
  → Original-Developer finden
  → Task zuweisen
  → Push: Reject-Nachricht an Developer via chat_send()
  → Fallback: pending_dispatch Queue
```

## 7. Undispatched Recovery (Watchdog)

```
_check_undispatched_tasks() (alle 30s):

  Findet "verlorene" Tasks die nie gesendet wurden:
    → Tasks mit status IN (in_progress, review)
    → assigned_agent_id IS NOT NULL
    → dispatched_at IS NULL

  Kann passieren wenn:
    → Task manuell reassigned wurde (z.B. nach Review-Rejection)
    → Dispatch-Flow unterbrochen wurde
    → Agent-erstellter Task mit expliziter Zuweisung

  Sequentielles Dispatch (kein Batch-Overload):
    → Pro Agent: max 1 Task pro Zyklus (dispatched_agents Set)
    → Busy-Check: Agent hat schon dispatched in_progress Task? → Skip
    → Naechster Task kommt erst wenn aktueller fertig (→ review/done)

  Fuer jeden Task (der den Check besteht):
    → Agent laden (muss gateway_agent_id haben)
    → _build_dispatch_message() (mit vollem Kontext + Feedback)
    → chat_send() mit reset_session=True
    → Erfolg: dispatched_at = now()
    → Auto-ACK: wenn Task schon in_progress → ack_at = now()
    → Event: task.undispatched_recovery
```

## 7b. Re-Dispatch Nachrichten

```
_build_dispatch_message() erkennt Re-Dispatch automatisch:

  → Laedt letzte 3 Feedback-Kommentare (comment_type="feedback")
  → Hat Feedback? → Re-Dispatch Modus:
      → Titel: "KORREKTUR NOETIG: {task.title}" (statt "Neue Aufgabe")
      → Reviewer-Feedback prominent am Anfang
      → Agent sieht sofort was schiefgelaufen ist
  → Task schon in_progress? → ACK-Instruktion uebersprungen
      → Agent muss nicht nochmal in_progress setzen
```

## 8. Henry (Board Lead) Interventionen

Henry kann jederzeit eingreifen:

| Aktion | Wie | Effekt |
|--------|-----|--------|
| **Task reassignen** | PATCH assigned_agent_id | Sofort Push an neuen Agent |
| **Subtasks erstellen** | POST task mit assigned_agent_id | Sofort Push an Ziel-Agent |
| **Status erzwingen** | PATCH status: in_progress | Aktiviert Task ohne ACK |
| **Phase starten** | PATCH Parent → in_progress | Alle inbox-Subtasks werden dispatcht |

Henry ist der zentrale Orchestrator — alle nicht-zugewiesenen Tasks gehen durch ihn.
Er erstellt Subtasks und weist sie Cody/Rex zu.

## 9. ACK-Timeout & Eskalation (Task Runner)

```
task_runner.py → _check_dispatch_ack() (alle 60s):

  1. ACK-Timeout Check:
     → Tasks mit status=inbox + dispatched_at gesetzt + kein ack_at
     → dispatched_at > 10 Minuten her?
         → JA: _handle_ack_timeout()
             → Board Lead finden
             → Task re-assignen an Board Lead (oder anderen Agent)
             → emit_event("task.ack_timeout", severity="warning")
             → Neuer Dispatch via chat_send()

  2. Dispatch-Pending Check:
     → Tasks mit status=inbox + assigned_agent_id + kein dispatched_at
     → assigned_at > 15 Minuten her?
         → JA: _handle_dispatch_pending()
             → Retry: chat_send() an zugewiesenen Agent
             → Erfolg: dispatched_at = now()
             → Fehlgeschlagen: pending_dispatch Queue

  3. Circuit Breaker (Eskalation):
     → _handle_final_escalation() nach MAX_DISPATCH_ATTEMPTS (3):
         → emit_event("task.dispatch_exhausted", severity="error")
         → → Discord-Notification an den Operator (severity error = auto Discord)
         → Task bleibt inbox — manuelle Zuweisung noetig

Konstanten:
  ACK_TIMEOUT_MINUTES = 10
  DISPATCH_PENDING_TIMEOUT_MINUTES = 15
  MAX_DISPATCH_ATTEMPTS = 3
```

## Datenfluss-Diagramm

```
Task (inbox)
  │
  ▼
auto_dispatch_task()
  │
  ├─ Pre-assigned? ──YES──▶ direkt an Agent
  │
  ├─ find_dispatch_target()
  │   ├→ Board Lead
  │   ├→ Fallback Agent + Warning
  │   └→ Kein Agent → Warning Event
  │
  ├─ Agent busy? ──YES──▶ task_queue (Watchdog liefert nach)
  │
  └─ Agent frei:
       │
       ├─ chat_send() OK ──▶ Task bleibt inbox, dispatched_at = now()
       │                          │
       │                          ▼
       │                     Agent ACK? (PATCH status: in_progress)
       │                          │
       │                     ├─ JA: ack_at = now(), status = in_progress
       │                     │
       │                     └─ NEIN (10min): Task Runner eskaliert
       │                          → Re-Assign an Board Lead
       │                          → Nach 3 Versuchen: Discord an den Operator
       │
       └─ chat_send() FAILED ──▶ pending_dispatch Queue
                                     │
                                     ▼
                                Watchdog (30s)
                                Agent hat Session? ──▶ chat_send() nachholen
```

## Side-Effects

| Aktion | DB | Redis | SSE | RPC |
|--------|----|----|-----|-----|
| Task dispatched (push) | Task UPDATE (dispatched_at) | broadcast | task.auto_dispatched | chat_send() |
| Task dispatched (pending) | Task UPDATE | pending_dispatch + broadcast | task.auto_dispatched | - |
| Task ACK'd | Task UPDATE (status, ack_at, started_at) | broadcast | task.updated | - |
| ACK Timeout | Task UPDATE (re-assign) | broadcast | task.ack_timeout | chat_send() (neuer Agent) |
| Dispatch exhausted | ActivityEvent | broadcast | task.dispatch_exhausted | - |
| Task queued (busy) | - | task_queue + broadcast | task.dispatch_queued | - |
| Task dequeued | Task UPDATE (dispatched_at) | task_queue + broadcast | task.queue_dispatched | chat_send() |
| Pending delivered | Task UPDATE (dispatched_at) | pending_dispatch + broadcast | task.pending_dispatch_delivered | chat_send() |
| No agent found | ActivityEvent | broadcast | task.dispatch_failed | - |
| Board Lead offline | ActivityEvent | broadcast | task.dispatch_fallback | - |
| Review handoff | Task UPDATE | broadcast | task.review_handoff | chat_send() |
| Review rejected | Task UPDATE | broadcast | task.review_rejected | chat_send() |
| Undispatched recovery | Task UPDATE (dispatched_at) | broadcast | task.undispatched_recovery | chat_send() |

## Sicherheitsnetze (Zusammenfassung)

```
Ebene 1 — Sofort:     auto_dispatch_task() bei Task-Erstellung
Ebene 2 — 30s:        Watchdog: task_queue (Agent war busy, jetzt frei)
Ebene 3 — 30s:        Watchdog: pending_dispatch (RPC war down, jetzt online)
Ebene 4 — 30s:        Watchdog: undispatched_recovery (assigned aber nie gesendet)
Ebene 5 — 60s:        Task Runner: ACK-Timeout (10min) → Re-Assign
Ebene 6 — 60s:        Task Runner: Dispatch-Pending (15min) → Retry
Ebene 7 — Eskalation: Circuit Breaker (3 Versuche) → Discord an den Operator
```

Kein Task kann "verloren gehen" — es gibt immer einen Prozess der ihn aufgreift.

## Edge Cases

- **Pre-assigned Tasks**: Werden korrekt gepusht, Status bleibt inbox bis ACK
- **Race Condition**: Task mit assigned_agent_id → Agent laden + pushen (kein find_dispatch_target)
- **Board Lead Pflicht**: Board Lead sieht neue Tasks zuerst (bei nicht pre-assigned Tasks)
- **Kein Gateway**: Agents ohne `gateway_agent_id` werden uebersprungen
- **Queue Persistenz**: Redis Queues ueberleben Agent-Offline
- **Pending Limit**: Max 3 pending Tasks pro Watchdog-Zyklus (verhindert Blockade)
- **Board Lead Implicit ACK**: Wenn Board Lead einen Subtask erstellt (parent_task_id), wird der Parent-Task automatisch als ACK'd markiert
- **Agent-Busy Definition**: in_progress ODER inbox+dispatched_at (dispatched aber nicht ACK'd)
- **Circuit Breaker**: 3 fehlgeschlagene Dispatch-Versuche → severity=error Event → Discord-Notification
- **Backfill (Migration 0018)**: Bestehende in_progress Tasks bekommen dispatched_at = started_at, ack_at = started_at
- **Undispatched Recovery**: Tasks die in_progress/review sind aber dispatched_at=NULL haben, werden automatisch via Watchdog nachgesendet (z.B. nach manueller Reassignment). Sequentiell: 1 Task pro Agent pro Zyklus + Busy-Check + Auto-ACK
- **Re-Dispatch mit Feedback**: Dispatch-Messages erkennen Review-Feedback automatisch. Titel wird "KORREKTUR NOETIG", Feedback prominent am Anfang. ACK-Instruktion uebersprungen wenn Task schon in_progress
- **Sequentielles Dispatch**: Undispatched Recovery dispatcht max 1 Task pro Agent pro Zyklus. Agent bekommt naechsten Task erst wenn aktueller fertig. Verhindert Batch-Overload
