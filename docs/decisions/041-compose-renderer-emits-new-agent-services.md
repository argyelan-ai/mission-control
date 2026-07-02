# ADR-041 — Compose-Renderer emittiert Service-Blöcke für neue cli-bridge-Agenten

**Status:** Accepted
**Datum:** 2026-06-23
**Scope:** Infra/Runtime

## Kontext

`compose_renderer.render_compose_agents` hat bisher nur **Image-Overrides + Vault-Mounts** auf die im statischen Template (`docker/docker-compose.agents.yml`) bereits vorhandenen Agent-Anchor-Services überlagert (`_rewrite_compose`). Ein **neu via API/Provisioning angelegter** cli-bridge-Agent (DB-Row + Token + Workspace) bekam dadurch NIE einen Compose-Service → kein Container → der Runtime-Switch scheiterte mit `no such service: mc-agent-<slug>` + Rollback. Damit war das Anlegen eines 9. (10., …) Fleet-Agenten keine reine Daten-/API-Operation, sondern erforderte stets ein Hand-Edit am generator-managed Compose-File.

Konkret aufgetreten beim Anlegen des dedizierten `estrich-vision`-Agenten (Plan-Vision für den Estrich-Kalkulator).

## Entscheidung

`render_compose_agents` hängt nach dem bestehenden Overlay für **jeden cli-bridge-Agenten, dessen `mc-agent-<slug>`-Service noch nicht im Template existiert, einen vollständigen Service-Block an** — anchor-basiert (`*claude-agent-base` bzw. `*openclaude-agent-base` je nach aufgelöstem Image), mit den Standard-Env-Vars (AGENT_NAME, MC_API_URL, `MC_TOKEN=${MC_TOKEN_<ENVKEY>}`, AGENT_VAULT_*, AGENT_SLUG) und Volumes (claude-config, mc-servers:ro, workspaces/<slug>, deliverables/<slug>, optional vault:rw). Bestehende Anchor-Services bleiben byte-identisch (durch Test abgesichert).

## Alternativen

- **Hand-Edit am Compose-Template pro neuem Agent:** Verworfen — das File ist generator-managed (wird beim nächsten Switch überschrieben), fehleranfällig, kein API-Self-Service.
- **Reine API-Operation ohne Container (manual runtime):** Verworfen — dann läuft kein pollender Claude-Code-Agent; für Vision-Tasks (Bild lesen) braucht es einen echten cli-bridge-Container.

## Konsequenzen

### Positiv
- Neue cli-bridge-Agenten sind jetzt vollständig über Create → Provision → Runtime-Switch bringbar (Self-Service), inkl. Container.
- Fleet ist beliebig erweiterbar ohne Template-Hand-Edits.

### Negativ / Risiken
- Renderer steuert jetzt die Service-Definition neuer Agenten — abgesichert durch Tests (`test_compose_renderer_new_agents.py`): die 8 bestehenden Services unverändert, neuer Block korrekt, YAML valid, kein Duplikat.

## Verifikation
6 neue Tests grün; Gesamtsuite 903 passed (7 pre-existing hermes_skill-Fails unrelated). Live: `estrich-vision` bekam `mc-agent-estrichvision` + laufenden Container, verarbeitet Vision-Tasks.
