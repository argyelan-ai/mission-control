# ADR-008 — Phase-Completion via Watchdog (zentral orchestriert)

**Status:** Accepted
**Datum:** 2026-02
**Scope:** Backend/Watchdog

## Kontext

MC nutzt ein Phasen-System: Ein Parent-Task repräsentiert eine Phase, Subtasks sind die konkreten Arbeiten. Beispiel:
- Parent: "Phase 1: Backend Setup"
  - Subtask 1: "Datenbank-Modell erstellen"
  - Subtask 2: "API-Endpoint bauen"
  - Subtask 3: "Tests schreiben"

**Frage:** Wenn alle Subtasks `done` sind, wer setzt den Parent-Task auf `review`?

Optionen:
- **Agent-getriggert**: Der letzte Agent der einen Subtask abschliesst checked die Geschwister und setzt Parent
- **Backend-Event-getriggert**: Bei jedem `task.status_changed` Event wird geprüft
- **Watchdog-zentral**: Zentraler Prozess prüft alle 30s

## Entscheidung

**Watchdog** (`watchdog/task_monitor.py`) prüft alle 30s. Für jeden Task mit `parent_task_id`:
- Sind alle Geschwister `done`?
- Wenn ja: Parent-Task → `review` (wenn `require_review`) oder direkt `done`
- Emit Event `phase.completed`, notify Board Lead + Reviewer

Watchdog ist **Single-Instance via Redis-Lock** (`mc:watchdog:lock`) — auch bei Multi-Worker-Backend läuft nur eine Instanz gleichzeitig.

## Alternativen

- **A: Agent-getriggert** → verworfen weil:
  - **Distributed Invariant**: Agent weiss nicht wer seine "Geschwister" sind (N+1 Query nötig)
  - **Race Conditions**: Wenn 2 Agents gleichzeitig den letzten Subtask abschliessen (Phase mit 2 parallel laufenden Zweigen), beide setzen Parent → Doppel-Trigger
  - **Scope-Problem**: Worker-Agents haben nicht unbedingt `tasks:manage` Scope um Parent-Task zu updaten
- **B: Backend-Event-getriggert** (bei jedem `PATCH status: done`) → verworfen weil:
  - Latency zwar besser, aber Event-Handler läuft im Request-Context → Race bei parallelen Requests
  - Keine Möglichkeit für asynchrone Recovery wenn Event verloren geht
  - Mehr Code-Pfade für dieselbe Logik (mehrere Handler)
- **C: DB-Trigger** → verworfen weil:
  - Business-Logik in DB-Triggern = schwer testbar, schwer versionierbar
  - Postgres-spezifisch
  - Kein Eventing ausserhalb der DB möglich

## Konsequenzen

### Positiv
- **Centralized Decision**: Watchdog ist single orchestrator, alle Phase-Decisionen gehen durch ihn
- **Race-free**: Redis-Lock + single-pass Loop → keine doppelten Trigger
- **Idempotent**: Wenn Watchdog abstürzt und neu startet, checks passieren neu (kein Verlust)
- **Recovery**: Wenn Event verloren ging, Watchdog holt beim nächsten Tick nach
- **Testbar**: Watchdog kann isoliert getestet werden (mock Time, mock DB)
- **Einfachere Agent-Scopes**: Agents brauchen kein `tasks:manage`, nur `tasks:write` für eigene Tasks

### Negativ
- **Latenz**: Max 30s bis Phase-Review startet nach letztem done-Subtask (akzeptabel für AI-Workflows)
- **Watchdog-Availability**: Wenn Watchdog ausfällt, Phase-Completion stuck bis Restart
- **Monitoring nötig**: Watchdog-Loop-Health ist eigene Sache (siehe Watchdog-Health-Checks)
- **Polling-Overhead**: Alle 30s N Tasks scannen — bei 1000+ aktiven Tasks wird das teuer (noch nicht erreicht)

## Referenzen

- Code: `backend/app/services/watchdog/task_monitor.py` (Phase-Completion-Check)
- Core: `backend/app/services/watchdog/core.py` (Singleton + Redis-Lock)
- Events: `emit_event("phase.completed", ...)` in `activity.py`
- Verwandt: ADR-001 (ACK-Handshake — auch via Task Runner Loop), ADR-009 (Agent-Scopes — Agents können Parent nicht selbst updaten)
