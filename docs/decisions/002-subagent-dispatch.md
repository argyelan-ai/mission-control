# ADR-002 — Subagent Dispatch mit Kill-Switch

**Status:** Accepted
**Datum:** 2026-03
**Scope:** Backend/Dispatch

## Kontext

Ursprünglich lief jeder Agent in **einer einzigen persistenten Session** (via `chat_send()`). Das bedeutete:
- **Serielle Arbeit**: Agent konnte nur einen Task nach dem anderen abarbeiten (Session-Sperre)
- **Context-Pollution**: Alte Tasks hingen im Session-Verlauf, "Lost in the Middle" Effekt → Agent vergass Details
- **Queue nötig**: Backend musste Tasks in Redis FIFO zwischenlagern wenn Agent busy war
- **Review-Rejection kompliziert**: Bei Ablehnung musste die komplette Session neu geladen werden

## Entscheidung

**Zwei Dispatch-Modi** parallel:

1. **Board Lead (Henry)** — `rpc.chat_send()`: Haupt-Session, persistent, voller Kontext (Orchestrator sieht alles)
2. **Worker (Cody, Rex, andere)** — `rpc.chat_send_isolated(session_key)`: Task-isolierte Session pro Task

**Session-Key-Schema:**
- Developer: `agent:{agentId}:task:{taskId}:work`
- Reviewer: `agent:{agentId}:task:{taskId}:review`

**Review-Rejection**: REUSE desselben `:work`-Keys (Kontext bleibt erhalten, neuer Dispatch mit `dispatch_intent=review_rework`)

**Kill-Switch**: Env `USE_SUBAGENT_DISPATCH=true` (Default). Auf `false` umgestellt → sofortiger Legacy-Modus via `chat_send()` + Redis FIFO Queue. Legacy-Code bleibt vollständig erhalten.

**Gateway-Config**: `maxConcurrent: 1` (ein Gateway-Slot pro Agent, aber unbegrenzt viele isolierte Sessions).

## Alternativen

- **A: Nur `chat_send()` bleiben** → serielle Arbeit, Context-Pollution
- **B: Multiple Sessions ohne Key-Scheme** → race conditions, keine saubere Review-Rework
- **C: Komplett neues Session-Modell** → zu invasiv, keine Rollback-Option
- **D: Ein Agent pro Task starten** → zu ressourcenintensiv, Gateway-Provisioning langsam

## Konsequenzen

### Positiv
- **Parallelisierung**: Workers können theoretisch gleichzeitig an mehreren Tasks (isolierte Sessions)
- **Kleinere Context-Größe**: Jeder Task bekommt sauberen Kontext, weniger "Lost in the Middle"
- **Review-Rework klappt**: `:work`-Key reuse, Kontext bleibt
- **Fallback-Sicherheit**: Kill-Switch ermöglicht Rollback in Sekunden
- **Graduelle Einführung**: Konnte erst für Planner, dann Developer, dann alle getestet werden
- **Board Lead bleibt Single Brain**: Henry hat alle Board-Entscheidungen in einer Session (gute Übersicht)

### Negativ
- **Komplexerer Dispatch-Code**: `is_board_lead` Check an vielen Stellen in `dispatch.py` + `task_runner.py` + `watchdog/`
- **Zwei Code-Paths**: Legacy + Subagent parallel gewartet → mehr Test-Oberfläche
- **Session-Key-Management**: Ungültige/alte Session-Keys können Leaks erzeugen
- **Gateway-Load**: Mehr Session-Spawns pro Stunde → höhere RPC-Last

## Referenzen

- Implementation: `backend/app/services/dispatch.py`, `backend/app/services/openclaw_rpc.py` (`chat_send_isolated`)
- Kill-Switch: `.env` → `USE_SUBAGENT_DISPATCH=true|false`
- Watchdog: `backend/app/services/watchdog/task_monitor.py` (überspringt `current_task_id`-Tracking im Subagent-Modus)
- Verwandt: ADR-001 (Dispatch ACK), ADR-005 (Board-Lead-First), ADR-007 (Structured Messages)
