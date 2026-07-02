# ADR-027 — Universal Agent ↔ Runtime Binding (atomic switch + image-aware lifecycle)

**Status:** Accepted
**Datum:** 2026-04-28
**Scope:** Backend/Runtime, Backend/DB, Frontend/Runtimes
**Erweitert:** ADR-018 (Runtime-Wechsel via Container-Restart)

## Kontext

ADR-018 hat den Same-Image Runtime-Wechsel via `docker restart` etabliert. Im Praxisbetrieb hat sich aber ein zweites, stillschweigend kaputtes Szenario gezeigt:

Wenn der Operator einen Agent von `cloud` (claude-Binary, `mc-claude-agent:latest`) auf `vllm_docker` / `lmstudio` / `openai_compatible` (`mc-agent-base:latest` mit openclaude) switcht, ist das ein **Image-Wechsel** — aber `docker/docker-compose.agents.yml` war hardkodiert via YAML-Anchors (`<<: *claude-agent-base` vs `<<: *openclaude-agent-base`). Ein einfacher `docker restart` lädt das Image nie neu, der Container startet weiter mit dem alten Binary, und die neuen `OPENAI_BASE_URL` Env-Vars werden vom claude-Binary entweder ignoriert oder brechen den Start.

Zusätzlich war der bisherige PATCH-Pfad nicht atomar: bei Health-Fail nach Restart blieb die DB im neuen Zustand, die Files reflektierten die neue Runtime, aber der Container war kaputt — und es gab kein Rollback. Concurrent PATCHes konnten sich überholen, in-progress Switches überrollten aktive Tasks ohne Confirm.

Drei Optionen standen im Raum:

1. **Static fix per Hand:** Der Operator editiert `docker-compose.agents.yml` bei jedem Cross-Image Switch. Verworfen — händische YAML-Edits sind fehleranfällig und brechen den UI-Flow.
2. **Eigene Image-Layer pro Agent:** jedes Image vorbauen, Switch = Image-Tag wechseln. Verworfen — nur 2 Image-Klassen (claude vs openclaude), Mehraufwand ohne Mehrnutzen.
3. **DB-driven compose generation + atomic switch service:** `agent.runtime_id` ist Single Source of Truth. Ein Renderer generiert `docker-compose.agents.yml` aus dem DB-State; ein Switch-Service orchestriert DB → Files → Image → Container in einer Transaktion mit Rollback. **Gewählt.**

## Entscheidung

`agent.runtime_id` (FK auf `runtimes.id`) ist die alleinige Wahrheit für die Runtime-Bindung. Daraus folgt der Image-Tag (`compose_renderer.pick_image_for_runtime`), die `.env`-Datei (`docker_agent_sync.sync_docker_agent_files`) und der Restart-Modus (`force_recreate=image_change`). Der Switch läuft atomar durch `services/agent_runtime_switch.switch_agent_runtime()`:

1. Validate (runtime exists, enabled, agent is cli-bridge, optional compatibility soft-warnings).
2. Optional in-progress Block (`AgentBusyError` mit Force-Toggle in UI).
3. Snapshot old state.
4. Acquire Redis lock `mc:agent:{id}:runtime-switch` (TTL 120s, prevents concurrent switches).
5. Bei Image-Change: render neue compose-Datei BEVOR Container angefasst wird.
6. DB-Commit `agent.runtime_id`.
7. `sync_docker_agent_files` re-rendert `.env` + `settings.json`.
8. `restart_docker_agent_container(force_recreate=image_change)` → entweder `docker restart -t 5` oder `docker compose up -d --force-recreate`.
9. `wait_for_agent_healthy` (30s same-image / 90s recreate).
10. On any failure: full rollback (DB + files + compose + container) + `agent.runtime_switch_failed` Event.
11. On success: `agent.runtime_switched` Event + Redis publish auf `mc:agent:{id}:terminal:remount` damit die Sessions-Seite den WebSocket re-mountet.

Dry-run Variante (`POST /agents/{id}/preview-runtime-switch`) liefert dieselbe `SwitchResult`-Shape ohne Mutation und treibt das Confirm-Modal im Frontend.

## Alternativen

- **`docker compose --build`:** statt force-recreate komplett neu bauen. Verworfen — wir wechseln zwischen 2 vorgebauten Images, nicht zwischen Image-Varianten desselben Builds.
- **Per-Agent Image-Tag:** jedes Image pro Agent eindeutig taggen (z.B. `mc-agent-davinci:claude` vs `mc-agent-davinci:openclaude`). Verworfen — Storage-Overhead, kein Mehrwert; die 2 Basis-Images decken alle 10 Agents ab.
- **Hot-Reload openclaude:** würde nicht über die Image-Grenze (claude ↔ openclaude) gehen, also löst es nur das Same-Image-Problem, das bereits funktioniert.
- **Optimistisches Locking auf der Agent-Row:** statt Redis-Lock. Verworfen — der Lock muss auch konkurrierende Subprocess-Restarts (docker compose) abdecken, nicht nur DB-Schreibvorgänge; Redis ist die natürliche Stelle.

## Konsequenzen

### Positiv
- **Cross-CLI Switches funktionieren** (claude ↔ openclaude). Das war zuvor silent broken — DB sagte "switched", Container lief mit altem Binary.
- **Atomar mit Rollback:** Health-Fail oder Compose-Render-Fail rollen DB + Files + Image + Container zurück.
- **Concurrency-safe:** Redis-Lock + 120s TTL.
- **Auditierbar:** `agent.runtime_switched` / `agent.runtime_switch_failed` Activity-Events mit alter/neuer Runtime, Image-Switch-Flag, Dauer, Warnings.
- **UI gewinnt Substanz:** Dry-run Preview, Compatibility-Warnings, Force-Toggle bei aktiver Task, automatischer Sessions-Remount, Bound-Agents Footer auf RuntimeCards mit Bind-Modal.
- **`docker-compose.agents.yml` wird Generator-managed:** händische Edits sind nicht mehr nötig (und werden überschrieben), aber `.bak`-Backup wird vor jedem Schreiben erstellt.

### Negativ
- **30–90s Latenz bei Image-Switch** (`docker compose up -d --force-recreate` muss das Image laden + Container neu erstellen). Same-Image bleibt bei ~5s.
- **`docker-compose.agents.yml` darf nicht mehr by-Hand editiert werden** — der nächste Switch überschreibt es. ADR + Backup-File mitigieren.
- **Lock kann hängen** wenn ein Switch crashed ohne Lock-Release — TTL 120s räumt automatisch auf, aber der Operator muss bei Edge-Cases evtl. `redis-cli del mc:agent:{id}:runtime-switch` rufen.
- **Subprocess-Calls aus dem Backend-Container:** `docker compose` muss vom Backend aus erreichbar sein (Mac → docker.sock-Mount), war aber bereits Voraussetzung für die existierende Restart-Pfad.

## Implementierung

- `backend/app/services/compose_renderer.py` — `pick_image_for_runtime`, `detect_image_change`, `render_compose_agents`, `write_compose_agents` (atomic write + .bak).
- `backend/app/services/docker_agent_sync.py` — `restart_docker_agent_container(force_recreate=)`, `wait_for_agent_healthy`.
- `backend/app/services/agent_runtime_switch.py` — `switch_agent_runtime`, 6 typed exceptions, validate_compatibility, is_agent_busy, terminal_remount publish.
- `backend/app/routers/agents.py` — PATCH delegiert an Switch-Service; `POST /agents/{id}/preview-runtime-switch` für dry-run; `GET /agents/{id}/terminal-events/stream` für Sessions auto-remount.
- `frontend-v2/src/components/shared/RuntimeSwitchModal.tsx` — Confirm-Modal mit Preview + Force-Toggle.
- `frontend-v2/src/components/shared/BindAgentModal.tsx` + RuntimeCard Footer — `/runtimes` Bound-Agents UI.
- `frontend-v2/src/components/shared/RuntimePill.tsx` — extracted, default + compact variant.
- `frontend-v2/src/hooks/useTerminalRemountSignal.ts` — SSE consumer für Sessions auto-remount.

Tests: 14 backend (`tests/test_agent_runtime_switch.py`) + 8 frontend vitest.

## Verwandte ADRs

- **ADR-018** (Runtime-Wechsel via Container-Restart) — Vorgänger; ADR-027 erweitert um Image-Lifecycle, Atomicity, Rollback, UI Hardening.
- **ADR-006** (Single Source of Truth: Templates → DB → Files) — gleiche Linie, hier ergänzt um Image-Tag im docker-compose.
- **ADR-017** (Runtime Registry in DB) — liefert das Schema, das ADR-027 als Single Source of Truth nutzt.
- **ADR-003** (Triple-Runtime-Architektur) — definiert host / openclaw / cli-bridge; nur cli-bridge ist switchable.
