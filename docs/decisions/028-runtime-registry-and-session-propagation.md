# ADR-028 — Runtime Registry Konsolidierung + Session-Env-Propagation

**Status:** Accepted (D-22 superseded by [ADR-053](053-runtime-watcher.md), 2026-07-05)
**Datum:** 2026-04-29
**Scope:** Backend/Runtime, Backend/DB, Frontend/Runtimes
**Erweitert:** ADR-027 (Universal Agent ↔ Runtime Binding)

## Kontext

ADR-027 hat den atomaren Cross-Image-Switch (claude ↔ openclaude) etabliert und damit das größere Problem gelöst. Im Praxisbetrieb haben sich danach drei kleinere, aber unangenehme Lücken gezeigt:

1. **Registry-Dualismus.** `GET /api/v1/runtimes` und `GET /runtimes/{id}` lasen weiterhin aus `runtime_manager.load_registry()` (JSON-Datei `backend/config/runtimes.json`), während alle Mutationen seit Phase 15 über die DB-Tabelle `runtimes` liefen. Resultat: Display-Name + Endpoint konnten in DB editiert werden, GET-Handler servierten aber stale JSON-Werte. Beim Live-Verify in Phase 16 fiel auf, dass `qwen-general` in DB das alte vLLM-Modell auf totem Port 8001 zeigte, der JSON-Seed das richtige auf 8000 — keine Mutation hatte je synchronisiert.

2. **Same-Image Switch zu langsam.** ADR-027 nutzt für jeden Switch `force_recreate=image_change`. Ein Wechsel **innerhalb derselben Image-Klasse** (z.B. zwei verschiedene vLLM-Modelle) braucht aber gar keine neue Container-Instanz — nur frische Env-Vars in der laufenden openclaude- bzw. claude-Session. `force_recreate` killt dabei poll.sh (Window 1) + Recycler (Window 2) mit, kostet 30–90s und führt zu ACK-Lücken im Watchdog.

3. **Frontend-Cache-Inkohärenz.** TanStack-Query-Caches mit `staleTime: 30_000` zeigten bis zu 30s veraltete Runtime- und Agent-Daten nach Mutationen. Insbesondere `RuntimeSwitchModal`-Preview holte Probe-Daten aus dem Cache statt frisch.

Drei zusätzliche Anforderungen waren über die Lücken hinaus offen: (a) Bootstrap-Routing für Token-Vars (`ANTHROPIC_AUTH_TOKEN` für claude-Image vs. `OPENAI_API_KEY` + `OPENAI_BASE_URL` für openclaude-Image) musste isoliert testbar werden — vorher verteilt in `routers/internal.py` ohne Unit-Tests; (b) der Operator wollte ohne Switch einen vLLM/LM-Studio-Endpoint manuell re-probnen können (Re-probe-Button für Reload-Szenarien); (c) ein generischer Background-Probe alle 60s wurde ausdrücklich verworfen.

## Entscheidung

Phase 16 erweitert ADR-027 in fünf abgegrenzten Punkten:

1. **DB ist alleinige Wahrheit für die Runtime-Liste.** `GET /runtimes` und `GET /runtimes/{id}` lesen über `runtime_manager.list_db_runtimes(session)` aus der `runtimes`-Tabelle. `load_registry()` bleibt erhalten — wird aber nur noch beim Lifespan-Bootstrap als Seed-Quelle verwendet (`main.py`). Migration 0094 prüft idempotent: für jeden Slug aus `runtimes.json` ohne DB-Row wird ein INSERT mit den JSON-Defaults ausgeführt; existierende Rows werden niemals UPDATE-d oder DROPPED.

2. **Same-Image-Switch via tmux respawn-window.** `restart_docker_agent_container` bekommt einen dritten Modus `respawn_window_only=True`, der `docker exec mc-agent-{slug} tmux respawn-window -k -t {slug}:0` ausführt. Window 1 (poll.sh) und Window 2 (recycler.sh) bleiben unangetastet. `wait_for_agent_healthy(respawn_mode=True)` pollt `tmux capture-pane` bis ein Ready-Signal (`╭─` Header oder `❯` Prompt) auftaucht. Bei multi-model Endpoints klickt `_wait_for_window_ready` einmalig den Modell-Picker mit Enter weg. Der Switch-Service wählt den Modus per `detect_image_change(old_runtime, new_runtime)`: Image-Wechsel → `force_recreate=True`, sonst `respawn_window_only=True`.

3. **`build_runtime_env(runtime)` Helper isoliert.** Routing-Logik aus `routers/internal.py` extrahiert in `runtime_manager.build_runtime_env(rt)` mit fünf Unit-Tests: claude-Image (`anthropic_*` runtime_type) → setzt `ANTHROPIC_AUTH_TOKEN`; openclaude-Image (`vllm_docker` / `lmstudio` / `openai_compatible` / `unsloth` / `cloud`) → setzt `OPENAI_API_KEY` + `OPENAI_BASE_URL`. `agent_bootstrap` ruft den Helper auf statt selbst zu routen.

4. **Frontend Cache-Invalidation.** `staleTime: 0` für `runtime-switch-preview` in `RuntimeSwitchModal`. Nach jeder Runtime-Mutation und nach jedem Switch invalidiert der Mutation-`onSuccess` die Keys `["runtimes"]`, `["agents"]`, `["agent", agentId]` und `["runtime-switch-preview", agentId]`.

5. **`POST /api/v1/runtimes/{id}/probe-model` Endpoint.** Re-uses Phase-15 Probe-Logik (`probe_runtime_model`) gegen den Endpoint einer Runtime. Persistiert das Ergebnis (kein Cache). 422 für `cloud`-Runtimes, 404 für unbekannte Slugs/UUIDs. Auf Endpoints mit mehreren Modellen wird `data[0].id` als kanonisch gewählt. Re-probe-Button im Frontend triggert den Endpoint manuell — ein periodischer Background-Probe wird **nicht** eingeführt (D-22, deferred).

## Alternativen

- **Live-JSON statt DB.** `runtimes.json` weiter als SSoT, jede Mutation schreibt zurück. Verworfen — bricht atomare Transaktionen, blockiert Multi-Worker-Konsistenz, und die DB-Tabelle existiert seit ADR-017 ohnehin.
- **Container-Hot-Reload statt respawn-window.** openclaude und claude haben kein Public Reload-API. tmux respawn-window war die kleinste Lösung, die ohne Container-Neustart neue Env-Vars im Prozess landet.
- **`staleTime: Infinity` + manuelles Refresh.** Verworfen — der ganze Punkt ist, dass nach einer Mutation die Liste sofort frisch ist. `0` + targeted invalidate liefert das ohne Polling-Druck.
- **Periodisches Background-Probing aller Runtimes.** Verworfen (D-22) — Cost/Benefit ungünstig, der Operator probet bei Bedarf manuell. Re-probe-Button macht das auf einen Klick zugänglich. **Superseded 2026-07-05 by [ADR-053](053-runtime-watcher.md):** "engine leads, MC follows" turned out to require exactly the active observation D-22 rejected — the `RuntimeWatcher` now probes every 90s with two-probe drift confirmation.

## Konsequenzen

### Positiv

- **Same-Image Switches in <5s** statt 30–90s. poll.sh + recycler überleben den Switch.
- **Display-Name/Endpoint-Edits in der UI sind sofort wirksam** — keine Stale-Reads mehr aus JSON.
- **Token-Routing testbar isoliert** — `build_runtime_env` hat Unit-Tests, das Verhalten beim Cross-Image-Switch ist nicht mehr "läuft hoffentlich".
- **Re-probe-Button** löst den vLLM-Reload-Use-Case ohne Roundtrip über docker compose.
- **Migration 0094 idempotent** — Re-Apply auf bereits geseedeten Datenbanken ist no-op, kein Risiko bei Backups/Restore.

### Negativ

- **Two restart paths** (`force_recreate` + `respawn_window_only`) erhöhen die Test-Matrix. `detect_image_change` muss korrekt klassifizieren — Fehler in beide Richtungen sind unangenehm (zu viele Recreates → Watchdog-Lücken; zu wenige → Cross-Image-Switch silent broken).
- **Backend braucht Host-Pfad-Mounts.** Cross-Image-Switches rufen `docker compose -f .../docker-compose.yml -f .../docker-compose.agents.yml up -d --force-recreate <service>` aus dem Backend-Container. Die compose-Files + `.env` müssen unter dem absoluten Host-Pfad ins Backend gemountet werden, und `HOME` muss beim Subprocess-Call auf `$HOME_HOST` gezwungen werden, sonst expandiert docker compose `${HOME}` zu `/home/mcuser` und der Daemon verweigert Mounts.
- **Backend-Image braucht `docker-compose-plugin`** zusätzlich zu `docker-ce-cli`. Bare CLI ohne Plugin parst `docker compose ...` nicht.

### Live-Verifikation (D-13, 2026-04-29)

Beim Live-Verify in Phase 16 wurden vier Phase-15-Restbestände gefunden und gefixt; alle in Commits `b5a98432`, `f7566696`, `deabc96c` (Plus die Wave-1-3-Code-Commits) eingefangen:

- `compose_renderer` brauchte Mounts für `docker-compose.yml`, `docker/docker-compose.agents.yml` und `.env` unter dem Host-Pfad in den Backend-Container.
- `docker compose` Plugin fehlte im Backend-Image.
- `HOME`-Substitution musste explizit auf `$HOME_HOST` gesetzt werden.
- RESEARCH.md Pitfall-3 war falsch: tmux-Session = `slug` (lowercase, aus `AGENT_NAME` Env-Var in `docker-compose.agents.yml`), nicht `agent.name`. `_respawn_agent_window` und `_wait_for_window_ready` korrigiert.

Plus zwei UX-Korrekturen im Wait-Loop: openclaude druckt `❯` mit U+00A0 statt regulärem Space (Match auf bare `❯`), und ein Modell-Picker bei Multi-Model-Endpoints wird automatisch mit Enter dismissed. Cross-CLI-Switch wurde an Tester (claude → openclaude) und Same-Image-Switch an Sparky (vLLM Qwen → Ollama Cloud) live durchgespielt — beide Pfade laufen.

## Referenzen

- Plans: `.planning/phases/16-runtime-registry-sync-and-session-env-propagation/16-{01..05}-*-PLAN.md`
- Backend: `backend/app/services/runtime_manager.py:list_db_runtimes`, `backend/app/services/runtime_manager.py:build_runtime_env`, `backend/app/services/docker_agent_sync.py:_respawn_agent_window`, `backend/app/services/docker_agent_sync.py:_wait_for_window_ready`, `backend/app/services/docker_agent_sync.py:restart_docker_agent_container`, `backend/app/services/agent_runtime_switch.py`, `backend/app/routers/runtimes.py`, `backend/app/routers/internal.py`, `backend/alembic/versions/0094_runtime_registry_seed_check.py`
- Frontend: `frontend-v2/src/lib/api.ts:probeModel`, `frontend-v2/src/components/shared/RuntimeSwitchModal.tsx`, `frontend-v2/src/app/agents/[id]/page.tsx`, `frontend-v2/src/app/runtimes/page.tsx`
- Infra: `backend/Dockerfile` (docker-compose-plugin), `docker-compose.yml` (host-path mounts)
- Verwandte ADRs: ADR-027 (atomic switch + image-aware lifecycle), ADR-017 (DB-backed runtime registry), ADR-018 (Container-Restart als Switch-Mechanismus)
