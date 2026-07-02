# Task Lifecycle

> Vollstaendiger Lebenszyklus eines Tasks von der Erstellung bis zum Abschluss.

## Status-Flow

```
inbox → in_progress → review → done
                  ↘ blocked
                  ↘ failed
```

**Gueltige Transitions** (definiert in `routers/tasks.py:VALID_TRANSITIONS`):
- `inbox` → in_progress, blocked, done
- `in_progress` → review, done, blocked, failed
- `review` → in_progress, done
- `blocked` → inbox, in_progress
- `failed` → inbox, in_progress

## 1. Task-Erstellung

### Via User (Frontend)
```
routers/tasks.py:create_task()
  → POST /boards/{board_id}/tasks
  → DB: Task INSERT (status=inbox)
  → emit_event("task.created")
  → broadcast(SSE)
  → IF kein assigned_agent_id UND Board.auto_dispatch_enabled:
      → BackgroundTask: auto_dispatch_task(task_id, board_id)
```

### Via Agent (API)
```
routers/agent_scoped.py:agent_create_task()
  → POST /agent/boards/{board_id}/tasks
  → DB: Task INSERT (can set assigned_agent_id)
  → emit_event("task.created")
```

### Via Planner (Plan finalisieren)
```
routers/planner.py:finalize_plan()
  → POST /planner/{project_id}/finalize
  → _extract_and_create_tasks() → _parse_plan_to_phases()
  → Erstellt Parent-Task (Phase) + Subtasks
  → Phase 1 Subtasks: status=inbox + auto_dispatch_task()
  → Phase 2+ Subtasks: status=inbox (warten auf den Operator)
```

### Via Content-Pipeline
```
routers/content.py:create_pipeline()
  → POST /content/pipelines
  → Erstellt Tasks fuer Research, Writing, Review
  → TaskDependency: Writing depends_on Research, Review depends_on Writing
```

## 2. Auto-Dispatch + ACK Handshake

```
services/dispatch.py:auto_dispatch_task()
  → Pruefen: Board.auto_dispatch_enabled?
  → find_dispatch_target(session, task, board_id)
      → 1. Board Lead (is_board_lead=True + gateway_agent_id) — Prioritaet!
      → 2. Fallback: erster Agent mit gateway_agent_id
  → Agent gefunden?
      → Agent busy? (in_progress ODER inbox+dispatched_at)
          → JA: task_queue.enqueue_task() — Redis FIFO Queue
          → NEIN: Task.status BLEIBT "inbox"
                  → _build_dispatch_message() (mit Kontext + ACK-Instruktion)
                  → rpc.chat_send() — Nachricht an Agent via Gateway
                  → Erfolg: Task.dispatched_at = now()
                  → Fehlgeschlagen: pending_dispatch Queue
```

**ACK Handshake:**
- Task bleibt `inbox` bis Agent explizit `PATCH status: in_progress` sendet
- Bei ACK: `task.ack_at = now()`, `task.started_at = now()`, `task.status = "in_progress"`
- Kein ACK nach 10min → Task Runner eskaliert (Re-Assign, nach 3 Versuchen Discord)
- Neue Felder: `dispatched_at` (Nachricht gesendet), `ack_at` (Agent hat bestaetigt)

**Dispatch-Message Inhalt:**
- Task-Titel + Beschreibung + Projekt-Kontext
- Board Memory + Agent-Lessons + relevante Lessons + Intelligence
- ACK-Instruktion ("Bestaetige SOFORT mit PATCH status: in_progress")
- Kommentar-Protokoll (Update/Evidence/Next Format)
- Callback-URLs fuer Status-Updates

## 3. Status-Aenderungen

### User aendert Status
```
routers/tasks.py:update_task()
  → PATCH /boards/{board_id}/tasks/{task_id}
  → _enforce_board_rules():
      → require_review_before_done: inbox/in_progress → done wird geblockt (muss durch review)
      → only_lead_can_change_status: Nur Board Lead darf Status aendern
  → Timestamp-Updates:
      → → in_progress: started_at = now(), ack_at = now() (= ACK)
      → → done: completed_at = now()
  → IF status=in_progress UND parent_task_id:
      → Alle inbox-Subtasks des Parents dispatchen (Phase-Start)
  → emit_event("task.updated")
  → broadcast(SSE)
  → IF Pipeline-Task auf done: pipeline_sync.sync_pipeline_from_task_done()
```

### Agent aendert Status
```
routers/agent_scoped.py:agent_update_task()
  → PATCH /agent/boards/{board_id}/tasks/{task_id}
  → Gleiche Board-Rules + Timestamps
  → IF inbox → in_progress: task.ack_at = now() (= ACK Handshake)
  → IF → done: total_tasks_completed++
  → emit_event()
```

### Board Lead Implicit ACK
```
routers/agent_scoped.py:agent_create_task()
  → POST /agent/boards/{board_id}/tasks (mit parent_task_id)
  → Parent-Task laden
  → IF parent.status == "inbox" UND parent.assigned_agent_id == current_agent:
      → Parent automatisch ACK'd: status = "in_progress", ack_at = now()
      → (Board Lead bestaetigt implizit durch Subtask-Erstellung)
```

## 4. Phase-Completion (Watchdog)

```
services/watchdog.py:_check_phase_completions()
  → Alle 30s: Parent-Tasks mit status=in_progress pruefen
  → Alle Subtasks done?
      → JA: Parent → review
      → Board Lead via rpc.chat_send() benachrichtigen
      → emit_event("task.phase_completed")
```

## 5. Queue Processing (Watchdog)

```
services/watchdog.py:_process_task_queues()
  → Alle 30s: Agents pruefen (kein in_progress, kein inbox+dispatched_at)
  → task_queue.dequeue_task(agent_id)
  → Task gefunden?
      → Task.dispatched_at = now() (Status bleibt inbox!)
      → _build_dispatch_message()
      → rpc.chat_send()
      → Agent muss ACK senden (PATCH status: in_progress)
```

## Side-Effects

| Aktion | DB | Redis | SSE | Discord | RPC |
|--------|----|----|-----|---------|-----|
| Task erstellt | Task INSERT, ActivityEvent | Pub/Sub broadcast | task.created | Bei warning+ | - |
| Task dispatched | Task UPDATE, Agent UPDATE | Queue (wenn busy) | task.updated | - | chat_send() |
| Status geaendert | Task UPDATE, ActivityEvent | Pub/Sub broadcast | task.updated | Bei warning+ | - |
| Phase complete | Task UPDATE (parent) | Pub/Sub broadcast | task.updated | - | chat_send() (Lead notify) |
| Task done | Task UPDATE, Agent UPDATE | Pub/Sub broadcast | task.updated | - | - |

## Edge Cases

- **Race Condition bei Dispatch**: `auto_dispatch_task` laedt Task frisch aus DB, prueft ob immer noch inbox
- **Board Rules**: `require_review_before_done` verhindert Ueberspringen von Review
- **Pipeline-Sync Loop**: `sync_pipeline_from_task_done` prueft `pipeline.status == task.pipeline_stage` um Loops zu verhindern
- **ACK Timeout**: Task Runner prueft alle 60s: dispatched_at > 10min ohne ack_at → Re-Assign
- **Dispatch Pending**: assigned_agent_id gesetzt aber dispatched_at leer > 15min → Retry
- **Circuit Breaker**: 3 fehlgeschlagene Dispatch-Versuche → severity=error → Discord an den Operator
- **Phase-Start**: Wenn ein Parent-Task auf in_progress gesetzt wird, werden alle inbox-Subtasks automatisch dispatched
- **Implicit ACK**: Board Lead erstellt Subtask → Parent wird automatisch ACK'd
