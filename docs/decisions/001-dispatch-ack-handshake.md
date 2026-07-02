# ADR-001 — Dispatch ACK Handshake

**Status:** Accepted
**Datum:** 2026-02
**Scope:** Backend/Dispatch

## Kontext

Ursprünglich hat MC beim Dispatch sofort den Task-Status auf `in_progress` gesetzt, nachdem `chat_send()` die RPC-Message erfolgreich an das Gateway geschickt hatte. Das führte zu mehreren Problemen:

1. **Task-Verlust nach Agent-Restart**: Agent crashte nach Dispatch → Task stand auf `in_progress`, aber niemand arbeitete daran
2. **Gateway-Lags unsichtbar**: RPC-Message kam nicht an oder verzögerte sich → keine Möglichkeit das zu erkennen
3. **Halluzinierte Arbeit**: Agent konnte antworten "arbeite daran" ohne tatsächlich den Kontext gelesen zu haben
4. **Auto-Reassign gefährlich**: Wenn man bei Stale Progress automatisch reassigned, verliert man den Kontext des ersten Versuchs

## Entscheidung

Task bleibt nach Dispatch im Status **`inbox`** (nicht `in_progress`). Zwei neue Timestamps werden eingeführt:
- `dispatched_at` — Zeitpunkt als `chat_send()` erfolgreich war
- `ack_at` — Zeitpunkt als Agent explizit per PATCH `status: in_progress` bestätigt (= ACK)

Task-Runner (60s Loop) prüft: kein ACK nach 10min → erzeugt **Approval** für den Operator (nicht Auto-Reassign). Nach 3 fehlgeschlagenen ACK-Versuchen → Circuit Breaker, Discord-Notification an den Operator.

Jede Dispatch-Message enthält ACK-Instruktion explizit:
> "Bestätige SOFORT mit PATCH status: in_progress"

Zusätzlich wird ein `dispatch_attempt_id` (UUID) pro Dispatch-Versuch generiert, den der Agent als Header bei Status-Updates mitsenden muss. Verhindert stale Updates von einem vorherigen Dispatch-Versuch.

## Alternativen

- **A: Sofort auf in_progress setzen** (altes Verhalten) → verworfen weil Task-Verlust bei Agent-Crash
- **B: Auto-Reassign bei Timeout** → verworfen weil riskant (Kontext weg, doppelte Arbeit möglich)
- **C: Nur Notification, keine State-Änderung** → verworfen weil Task dann unentdeckt im inbox bleibt

## Konsequenzen

### Positiv
- **Garantierte Delivery-Confirmation**: Task wird nur als "in Arbeit" markiert wenn Agent tatsächlich geantwortet hat
- **Idempotenz**: Agent kann `next-task` mehrmals aufrufen — solange kein ACK, bekommt er denselben Task
- **10min Debugging-Fenster**: Ausfälle werden sichtbar bevor sie stumm verloren gehen
- **Operator-gated Recovery**: Eskalation an Mensch statt Auto-Magic, transparent
- **Audit Trail**: `dispatched_at` vs `ack_at` Lücke ist messbar (Intelligence kann Pattern erkennen)

### Negativ
- **Komplexerer Dispatch-Code**: Task-Runner + Watchdog müssen Timeouts tracken
- **Latency**: Bei langsamen Agents können ACK-Approvals unnötigerweise entstehen (der Operator muss abwinken)
- **Two-Phase**: Frontend muss `inbox + dispatched_at != null` vs `pure inbox` unterscheiden
- **`dispatch_attempt_id` Overhead**: Agent muss Header mitsenden, ansonsten 409 Conflict

## Referenzen

- Migration: `backend/alembic/versions/0018_dispatch_ack.py`
- Services: `backend/app/services/task_runner.py` (`_check_dispatch_ack()`)
- Dispatch: `backend/app/services/dispatch.py` (`_build_dispatch_message()`)
- Agent-seitig: `backend/app/routers/agent_scoped.py` (enforcement `dispatch_attempt_id`)
- Design-Doc: `docs/plans/2026-02-28-dispatch-ack-handshake-design.md`
- Verwandt: ADR-002 (Subagent Dispatch), ADR-007 (Structured Messages)
