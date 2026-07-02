# ADR-017 — Runtime Registry in der DB

**Status:** Accepted
**Datum:** 2026-04-19
**Scope:** Backend/DB, Backend/Runtime

## Kontext

Bis 2026-04-19 war die Liste der verfügbaren Modell-Runtimes (LM Studio, vLLM, Unsloth, Cloud-Provider) in `backend/config/runtimes.json` hart verdrahtet. Agents hatten keine Referenz auf eine Runtime — der LLM-Endpoint kam aus `docker-compose.agents.yml` (Sparky auf `192.0.2.10:1234`, alle anderen auf Ollama Cloud). Änderungen bedeuteten Code-Edit + Deploy.

Drei Treiber für die Änderung:

1. **Runtime-Switching via UI:** der Operator wollte Agents über das MC-Frontend zwischen LMS, vLLM, Unsloth und Cloud wechseln können — ohne docker-compose anzufassen.
2. **Opensource-Readiness:** MC wird bald quelloffen. Die hardgecodete IP `192.0.2.10` des Operators ist für andere User nutzlos; sie brauchen eigene Runtime-Konfiguration via UI.
3. **Unsloth-Integration:** Unsloth Studio kommt als vierter Inference-Pfad dazu. Die Liste der Runtime-Typen muss erweiterbar sein.

## Entscheidung

Runtimes leben authoritative in der DB-Tabelle `runtimes`. `backend/config/runtimes.json` bleibt als **Seed**: beim ersten App-Start (Migration 0077 + Seeder im lifespan) wird jeder JSON-Eintrag per `INSERT … WHERE NOT EXISTS` in die DB übernommen. Danach ist die DB Single Source of Truth — UI-CRUD, API, runtime_manager lesen von dort.

Agents bekommen eine optionale FK `agents.runtime_id → runtimes.id` (ON DELETE SET NULL). Nur `agent_runtime = "cli-bridge"` darf den Wert setzen; Boss (host) und Henry (openclaw) bekommen eine Validation-Rejection.

## Alternativen

- **JSON bleibt Source of Truth, UI schreibt JSON** — Schreibzugriff aus dem Backend-Container auf eine Config-Datei im Git-Repo ist fragil (Restart-Resistance, Volume-Permissions). Verworfen.
- **Runtime-Info direkt auf Agent-Tabelle** (endpoint, model als Strings pro Agent) — Duplikation über N Agents, keine zentrale Stelle um eine Runtime zu deaktivieren oder umzurouten. Verworfen.
- **Env-Variablen per Agent in docker-compose** — bleibt das heutige Chaos, keine UI-Kontrolle. Verworfen.

## Konsequenzen

### Positiv
- Der Operator kann Runtimes via UI anlegen/bearbeiten/deaktivieren (Phase 3 & 4).
- Agents verweisen auf eine Runtime statt auf eine hardcoded URL.
- Opensource-User können das System mit ihren eigenen Endpoints betreiben, ohne Code zu patchen.
- runtime_manager bleibt für Lifecycle-Operations (start/stop/health) einziger Owner — Registry-Lesen wird durch DB-Queries ersetzt ohne API-Bruch.

### Negativ
- Doppelte Wahrheit in der Übergangszeit: JSON-Seed + DB laufen nebeneinander. `runtime_manager.load_registry()` liest aktuell noch die JSON — der finale Umbau auf DB-Read ist follow-up Arbeit (Phase 1b).
- Migration 0078 ist eine Data-Migration und muss beide Seiten (Seed-Row + Agent-Link) atomar fixen für Fresh-Deploys.

## Migration

1. `0077_runtimes_table.py` — Tabelle + FK + Index
2. Seeder im Lifespan (`_seed_runtimes`): JSON → DB, idempotent via slug
3. `0078_link_sparky_runtime.py` — legt `qwen-coder-lms` Runtime defensiv an und linkt Sparky, falls bereits provisioniert

## Konsequenzen für Deployment

- Fresh Deploys: `alembic upgrade head` läuft idempotent; Seeder ergänzt fehlende Runtimes; Sparky wird automatisch verlinkt.
- Existing Deploys (Mac Mini des Operators): Migration 0078 setzt `agents.runtime_id = qwen-coder-lms` für Sparky, sofern NULL — d. h. kein Eingriff in manuelle UI-Zuweisungen.

## Follow-ups

- Phase 1b: `runtime_manager` komplett auf DB-Read umstellen (AsyncSession injection), `runtimes.json` auf Seed-Only reduzieren.
- Bootstrap-Wizard beim First-Start (UX-Hilfe für Opensource-User).
