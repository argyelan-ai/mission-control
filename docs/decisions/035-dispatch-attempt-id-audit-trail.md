# ADR-035 — `dispatch_attempt_id` Audit Trail + Race-Safe Initialisation

**Status:** Accepted
**Datum:** 2026-05-15
**Scope:** Backend/Dispatch

## Kontext

`tasks.dispatch_attempt_id` ist das Header-Token (`X-Dispatch-Attempt-Id`) das
poll.sh + cli-bridge nutzen, um *stale* Status-Updates eines Agenten zu er-
kennen: setzt der Agent z.B. `status: review` mit einer alten ID, wird der
PATCH stillschweigend abgelehnt. Das Feld wurde an **12 verschiedenen Stellen**
(5 Services, 7 Routers) direkt mutiert — als simples
`task.dispatch_attempt_id = str(uuid.uuid4()); session.add(task); commit()`.

Zwei Probleme manifestierten sich am 2026-05-15 (Researcher / "Wetter-
Staufen"-Vorfall):

1. **Race zwischen `auto_dispatch_task` und `/agent/me/poll`**: beide Pfade
   konkurrieren um die initiale Vergabe des `dispatch_attempt_id` während
   eines ~5s-Fensters (git-clone). Beide sahen `NULL`, beide setzten je
   eine UUID, der spätere Commit gewann (last-writer-wins). Folge: poll.sh
   bekam Header A ausgeliefert, Backend speicherte Header B → jeder Agent-
   PATCH wurde als stale verworfen.
2. **Forensik unmöglich**: nach dem Vorfall wussten wir, dass die ID ge-
   wechselt hatte, aber NICHT *welcher Code-Pfad* den Wechsel verursacht
   hatte. 30 Min Code-Archäologie endeten in einer Hypothese, keiner Ge-
   wissheit.

## Entscheidung

Jeder Schreibzugriff auf `tasks.dispatch_attempt_id` läuft ab sofort
ausschliesslich über die zwei Helper aus
`app/services/dispatch_attempt_audit.py`:

```python
await set_dispatch_attempt_id(
    session, task, new_id,
    caller="auto_dispatch",        # short identifier for forensic grouping
    reason="initial_dispatch",     # free-form context
    only_if_null=True,             # for race-prone init sites
)

await clear_dispatch_attempt_id(
    session, task, caller="task_lifecycle", reason="status_to_done",
)
```

Beide Helper:

1. Mutieren das Feld in derselben Transaktion wie ein `TaskAttemptAudit`-
   Insert (Migration 0116 legt die Tabelle an).
2. Loggen eine strukturierte Zeile (`mc.dispatch_attempt_audit`).
3. `set_dispatch_attempt_id(only_if_null=True)` macht ein konditionales
   `UPDATE … WHERE dispatch_attempt_id IS NULL` — first-writer-wins, race-
   frei. Verlierer sehen `False` als Return und refreshen die ORM-Instanz
   auf den kanonischen Wert.

Tabelle `task_attempt_audit`:

| Spalte | Typ | Inhalt |
|--------|-----|--------|
| `id` | UUID | PK |
| `task_id` | UUID | FK auf `tasks.id` (CASCADE on delete) |
| `old_attempt` | UUID | NULL für initiale Vergabe |
| `new_attempt` | UUID | NULL bei `clear_*` |
| `caller` | VARCHAR(64) | Code-Pfad-Identifier (z.B. `agent_poll`, `d1_silent_retry`) |
| `reason` | VARCHAR(256) | Freier Kontext |
| `created_at` | TIMESTAMPTZ | server-default `NOW()` |

Index `ix_task_attempt_audit_task_id_created_at` macht Forensik ein einziges
SQL-Statement:

```sql
SELECT created_at, caller, reason, old_attempt, new_attempt
FROM task_attempt_audit WHERE task_id = '<uuid>' ORDER BY created_at;
```

## Alternativen

- **Pessimistic Lock mit `SELECT … FOR UPDATE`** auf der `tasks`-Row beim
  Setzen → Verworfen weil sich das nicht auf das `auto_dispatch_task`-vs-
  `/agent/me/poll` Race anwenden lässt (zwei separate Sessions können
  trotzdem beide ein `SELECT` machen) und Lock-Eskalation in async-Pfaden
  Deadlock-Risiko hat.
- **Backend-Logging ohne separate Tabelle** → Verworfen weil Logs rotieren,
  Aggregation pro `task_id` aufwendig ist und SQL-basierte Forensik die
  Default-Operations-Tooling-Sprache ist.
- **Audit-Tabelle pro Mutation aller Felder generisch** (z.B. via Trigger)
  → Verworfen wegen Performance-Overhead + Komplexität bei selektivem
  Recovery; das `dispatch_attempt_id`-Feld ist ein hot-path-Spezialfall.
- **`compare_and_swap` über Redis** → Verworfen weil die Source-of-Truth
  Postgres ist und ein zweiter Speicher Inkonsistenz-Risiko addiert.

## Konsequenzen

### Positiv

- Race-freie Initialisierung: erste atomare `UPDATE`-Wins-Semantik schliesst
  das git-clone-Fenster.
- Vollständiger Audit-Trail: jede Mutation hat Zeitstempel, Caller, Reason,
  Old, New. Nächster ähnlicher Vorfall ist eine SQL-Query, kein
  Code-Walkthrough.
- Klare API für neue Caller: ein einziger Helper-Aufruf statt drei Zeilen
  Boilerplate (`task.dispatch_attempt_id = …; session.add; commit`).
- `clear_spawn_tracking()` (in `task_lifecycle.py`) **gibt die Verantwortung
  ab**: Caller, die das Feld geclearet haben wollten, müssen jetzt explizit
  `clear_dispatch_attempt_id` aufrufen — mit eigenem `caller`/`reason`,
  damit der Audit-Trail Aufrufkontext kennt.

### Negativ

- Eine zusätzliche Insert-Operation pro Mutation. Bei den ~50-100 Status-
  Wechseln pro Tag vernachlässigbar, aber bei einer hypothetischen Lastspitze
  mit 1000+ dispatchs/sec wäre die Audit-Tabelle der dichteste Hot-Pfad.
- Caller-/Reason-Strings sind ein Public Contract. Wenn wir den Wert eines
  `caller`-Strings in einem Refactor ändern, brechen Forensik-Queries (selten
  geschrieben aber wenn dann kritisch).
- `clear_spawn_tracking()` macht jetzt **weniger** als der Name andeutet —
  Caller müssen den dispatch_attempt_id-Clear bewusst dazu aufrufen.
- Migration `0116` muss vor jedem Backend-Restart gefahren sein, sonst
  500-Errors auf jedem Status-Update.

## Referenzen

- Betroffene Dateien:
  - `backend/app/services/dispatch_attempt_audit.py` (NEW, single source of truth)
  - `backend/app/models/task_attempt_audit.py` (NEW model)
  - `backend/alembic/versions/0116_task_attempt_audit.py` (NEW migration)
  - `backend/app/services/dispatch.py:517-540` (`auto_dispatch_task` race-fix)
  - `backend/app/services/operations.py:223-310` (stop/resume)
  - `backend/app/services/task_lifecycle.py:128-145, 297-303, 750-755, 843-848`
  - `backend/app/services/task_runner.py:353-365` (D-1 silent retry)
  - `backend/app/services/watchdog/task_monitor.py:569-581, 1474-1495`
  - `backend/app/routers/agent_comments.py:306-316` (auto-promote)
  - `backend/app/routers/agent_task_status.py:1267-1280` (subtask create)
  - `backend/app/routers/agents.py:3016-3032` (`/me/poll` race-fix)
  - `backend/app/routers/approvals.py:248-258, 791-799`
  - `backend/app/routers/tasks.py:1424-1430, 1481-1487`
- Commit: `2aae4788` — feat(dispatch): race-free dispatch_attempt_id with audit trail
- Verwandte ADRs: ADR-027 (Universal Agent Runtime Binding), ADR-031 (Hermes Hardening — Poll Claim)
- Tests: `backend/tests/test_dispatch_attempt_audit.py` (6 Cases), aktualisierte
  `test_operations.py::test_resume_rotates_dispatch_attempt_id`
