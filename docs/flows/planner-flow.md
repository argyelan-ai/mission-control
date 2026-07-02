# Planner Flow

> Chat-basierte Projektplanung: Von der Idee ueber Frage-Runden zum ausfuehrbaren Plan mit Phasen.

## Uebersicht

```
Der Operator startet Planung → Chat mit Planner-Agent → Fragen & Antworten → Plan finalisieren → Tasks erstellen
```

## 1. Planung starten

```
routers/planner.py:start_planning()
  → POST /planner/start
  → Body: { board_id, name, description, project_type }
  → Project INSERT (project_type="planner", status="planning", created_by="planner")
  → _find_planning_agent(session, board_id):
      1. Agent mit name="planner" + gateway_agent_id → Prioritaet
      2. Board Lead (is_board_lead=True) + gateway_agent_id → Fallback
      3. Erster Agent mit gateway_agent_id → letzter Fallback
  → System-Message erstellen (PLANNER_SYSTEM_PROMPT):
      - Rolle: Projektplaner
      - Instruktionen fuer strukturierte Planung
      - Board-Kontext (Name, Objective, Agents)
      - Output-Format fuer Phasen
  → PlannerMessage INSERT (role="system")
  → User-Nachricht mit Projektbeschreibung → PlannerMessage INSERT (role="user")
  → _send_and_capture_reply():
      → rpc.chat_send(planner_agent_id, system_prompt + user_message)
      → rpc.poll_agent_reply(max_attempts=15, interval=2s)
      → Deduplication: Pruefen ob Reply schon als PlannerMessage existiert
      → PlannerMessage INSERT (role="assistant")
```

## 2. Chat-Interaktion

```
routers/planner.py:send_message()
  → POST /planner/{project_id}/message
  → Body: { content }
  → PlannerMessage INSERT (role="user")
  → BackgroundTask: _send_and_capture_reply()
      → Chat-History laden (alle PlannerMessages des Projekts)
      → rpc.chat_send(planner_agent_id, full_conversation)
      → rpc.poll_agent_reply()
      → Deduplication Check
      → PlannerMessage INSERT (role="assistant")
      → broadcast(SSE: "planner.reply")
```

**Chat-History abrufen:**
```
routers/planner.py:get_chat()
  → GET /planner/{project_id}/chat
  → Alle PlannerMessages OHNE role="system"
```

## 3. Plan finalisieren

```
routers/planner.py:finalize_plan()
  → POST /planner/{project_id}/finalize
  → Body: { summary? }
  → Letzte Assistant-Message = Plan-Text
  → _extract_and_create_tasks(session, project, plan_text, board_id):
      → _parse_plan_to_phases(plan_text):
          → Regex-Parsing: "## Phase N:" oder "Phase N:" Pattern
          → Pro Phase: Titel + Liste von Subtasks (- oder * Prefix)
          → Returns: list[{phase_title, tasks: [{title, description}]}]
      → Pro Phase:
          → Parent-Task INSERT (title="Phase N: {phase_title}")
          → Pro Subtask:
              → Task INSERT (parent_task_id=parent.id)
              → IF Phase 1:
                  → Subtask.status = "inbox"
                  → BackgroundTask: auto_dispatch_task()
              → IF Phase 2+:
                  → Subtask.status = "inbox" (warten auf den Operator)
  → Project.status = "active"
  → Project.plan_summary = summary oder generiert
  → emit_event("project.planned")
```

## 4. Phasen-System

```
Phase 1 (sofort):
  → Subtasks: inbox + auto_dispatch
  → Agents arbeiten sofort

Phase 2+ (manuell):
  → Subtasks: inbox (KEIN auto_dispatch)
  → Der Operator muss Parent-Task auf in_progress setzen
  → Dann werden alle inbox-Subtasks dispatched (via tasks.py:update_task)
```

**Phase-Completion (via Watchdog):**
```
services/watchdog.py:_check_phase_completions()
  → Parent in_progress + alle Subtasks done?
  → Parent → review
  → Board Lead benachrichtigen
```

## Datenfluss

```
User Input
  ↓
PlannerMessage (role="user")
  ↓
rpc.chat_send() → OpenClaw Gateway → Planner Agent
  ↓
rpc.poll_agent_reply() ← Gateway ← Agent Response
  ↓
PlannerMessage (role="assistant")
  ↓
finalize_plan() → _parse_plan_to_phases()
  ↓
Parent-Tasks + Subtasks (DB)
  ↓
Phase 1: auto_dispatch_task() → Agent arbeitet
```

## Side-Effects

| Aktion | DB | Redis | SSE | RPC |
|--------|----|----|-----|-----|
| Planung starten | Project + PlannerMessages | - | - | chat_send() + poll |
| Nachricht senden | PlannerMessage | broadcast | planner.reply | chat_send() + poll |
| Finalisieren | Tasks (Parent + Subtasks) | broadcast | task.created | auto_dispatch (Phase 1) |

## Edge Cases

- **Deduplication**: `_send_and_capture_reply` prueft ob die Agent-Antwort schon als PlannerMessage existiert (Race Conditions bei mehreren Requests)
- **Kein Planner-Agent**: Fallback auf Board Lead, dann auf irgendeinen Agent mit Gateway
- **System-Prompt**: Wird bei jedem chat_send() mitgeschickt (nicht nur beim ersten Mal)
- **Plan-Parsing**: Regex-basiert — wenn Format nicht erkannt wird, wird ein einzelner Task ohne Phasen erstellt
- **Leerer Plan**: finalize_plan() braucht mindestens eine Assistant-Message

## Projekt-Typen

Definiert in `planner.py:PROJECT_TYPES`:
- feature, website, content, research, automation, design, free
