# ADR-018 — Runtime-Wechsel via Container-Restart (kein Hot-Reload)

**Status:** Accepted (Erweitert durch ADR-027 — Image-aware lifecycle + atomic switch + rollback)
**Datum:** 2026-04-19
**Scope:** Backend/Runtime, Infra/Agent

## Kontext

Wenn ein cli-bridge Docker-Agent seinen LLM-Runtime wechselt (`runtime_id` in der DB), muss der openclaude-Prozess im Container die neuen `OPENAI_BASE_URL` + `OPENAI_MODEL` übernehmen. openclaude liest diese env-Vars nur beim Prozess-Start — ein hot reload ist im aktuellen CLI nicht vorgesehen.

Zwei Varianten standen im Raum:

1. **Hot-Reload:** openclaude per Signal (SIGHUP) neu konfigurieren oder die API so erweitern, dass jeder API-Call den Ziel-Endpoint im Body mitgibt.
2. **Container-Restart:** `docker restart mc-agent-{slug}` nach jeder Runtime-Änderung; entrypoint.sh lädt alle env-Vars frisch aus `/internal/bootstrap` + `.env`.

## Entscheidung

Runtime-Änderung → `docker restart`. Implementiert in `PATCH /agents/{id}`: bei gesetztem `runtime_id` oder `?restart=true` wird `sync_docker_agent_files` + `restart_docker_agent_container` nach dem Commit aufgerufen.

UI zeigt einen Confirm-Dialog, weil die laufende Session beendet wird.

## Alternativen

- **Hot-Reload via SIGHUP:** openclaude unterstützt das nicht nativ. Eigener Patch im Upstream-Projekt wäre ein mehrtägiger Umweg mit unklarem Outcome. Verworfen.
- **Per-Request Endpoint-Override:** würde den API-Layer (openclaude → backend) komplizieren und jede Task-Dispatch-Message aufblähen. Und löst nicht das `OPENAI_API_KEY`-Problem (wenn die neue Runtime andere Auth braucht). Verworfen.
- **Nur `docker kill -s HUP`:** openclaude reagiert darauf nicht; in der Praxis äquivalent zum full restart, aber weniger vorhersehbar.

## Konsequenzen

### Positiv
- Ehrliche, nachvollziehbare Semantik: eine Änderung = ein sauberer Neustart.
- Keine Sonderlogik in openclaude / poll.sh / entrypoint.sh nötig.
- Auto-Restart des tmux-Watchdogs greift eh bei Container-Exit — die Codebasis war bereits für Restarts gebaut.

### Negativ
- Laufende Session geht verloren (~5 s Downtime pro Agent). UI warnt explizit davor.
- Tasks die gerade in flight sind, müssen vom Task-Runner re-dispatched werden (ACK-Timeout existiert bereits).

## Implementierung

- `backend/app/routers/agents.py::update_agent()` akzeptiert `runtime_id` + optional `?restart=true`.
- Nach DB-Commit: `sync_docker_agent_files(session, agent)` rendert `.env` mit neuen Werten, dann `restart_docker_agent_container(agent)` triggert `docker restart -t 5 mc-agent-{slug}`.
- Response enthält `_restart: {status, container}` für UI-Feedback.

## Verwandte ADRs

- ADR-003 (Triple-Runtime-Architektur) — definiert die Agent-Runtime-Typen.
- ADR-013 (Settings.json als echte Kopie) — gleiche Motivation: Docker-Mount-Kompatibilität statt komplexerer Sync.
- ADR-017 (Runtime Registry in DB) — liefert die Datenquelle für diesen Flow.
