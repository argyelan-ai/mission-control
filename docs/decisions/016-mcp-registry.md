# ADR-016: MCP Registry + Per-Agent Allowlist (Hybrid Method 1)

## Status
Accepted · 2026-04-18

## Context

Phase 1 ermöglicht Boss, Skills und Plugins install/uninstall-Requests zu stellen. Phase 2 erweitert das auf MCP-Server — aber MCP-Distribution hat andere Herausforderungen:

- **stdio-MCPs** brauchen Binaries im Container (Python, Node, native tools)
- **HTTP/SSE-MCPs** brauchen nur URL + Auth-Header
- **Pro-Agent-Zuweisung** muss konsistent mit Skill/Plugin-Pattern sein
- Existierende `mission-control`-MCP läuft auf Host — kein Konflikt mit Agent-Registry

## Decision

**Hybrid Method 1 (Registry + Allowlist + Docker-Mount):**

- Zentrale Registry in `~/.openclaw/mcp-servers/<name>/manifest.json` (MCPManifest-Schema: name, transport, command/args/env für stdio, url/headers für http/sse)
- Neues DB-Feld `agents.mcp_servers` (JSON) mit identischer Semantik wie `cli_skills`/`cli_plugins` (null=alle, []=keine, [...]=allowlist)
- `backend/app/services/mcp_registry.py` MCPRegistry service — CRUD + install (npm/github) + JSON-RPC smoke-test
- `backend/app/services/mcp_sync.py` mcp_sync — rendert `.mcp.json` pro Agent aus Registry + Allowlist
- Shared Docker-Mount: `~/.openclaw/mcp-servers:/mc-servers:ro` in allen Agent-Containern macht stdio-Binaries verfügbar
- Install-Executor bekommt MCP-Pfad mit Smoke-Test-Rollback (installiert → Smoke-Test fehlgeschlagen → DB + Registry rollback)
- Neue Approval-Types: `install_mcp`, `uninstall_mcp`
- User-Auth Admin-Endpoints: `GET /api/v1/mcp-servers`, `GET /api/v1/mcp-servers/{name}`, `PATCH /api/v1/agents/{id}/mcp-servers`

### Kernentscheidungen

1. **Shared-Mount statt Bake-Image**: `~/.openclaw/mcp-servers` read-only gemountet. Neue MCPs landen im Mount → sofort in allen Containern sichtbar, kein Rebuild nötig.
2. **npm install läuft auf HOST**: `MCPRegistry.install(npm:@pkg)` führt `npm install --prefix <registry-dir> <pkg>` auf dem Host aus. `node_modules/` landet im Mount-Pfad → Container sieht es unter `/mc-servers/<name>/node_modules/`.
3. **GitHub-MCPs brauchen manifest.json im Repo** — Phase-2-Anforderung, verhindert Guesswork für Command/Args. Phase-3 könnte heuristische Ableitung ergänzen.
4. **Smoke-Test vor Sync**: JSON-RPC initialize + tools/list. Fail → Auto-Rollback (DB + Registry-Uninstall). Verhindert "installierter aber kaputter" MCP.
5. **Identische Allowlist-Semantik**: `mcp_servers` Feld funktioniert exakt wie `cli_plugins`/`cli_skills` — null/[]/[...]. Null = Backward-Compat (alle verfügbaren).
6. **Admin-CRUD ist User-Auth**: der Operator managed via UI. Install-Flow für Agents läuft nur über Approval (Phase 1).
7. **mission-control MCP bleibt Host-lokal**: Nicht in Registry aufgenommen (Docker-Pfade passen nicht). Deferred zu Phase 3 (Gateway).

## Alternativen

- **MCP-Gateway-Proxy-Service** (Host-side Service proxyed MCP-Traffic): Defered zu Phase 3, YAGNI. 95%+ stdio-MCPs laufen mit Node/Python/uvx im `mc-agent-base` Image.
- **Bake-in Docker-Image**: Rebuild bei jedem neuen MCP, Agent-Restart bei jeder Installation. Ablehnend.
- **Runtime `claude mcp add`**: State-Drift zwischen DB und `~/.claude.json`. Ablehnend.
- **Git-tracked Registry**: MCP-Server-Code kann gross sein (node_modules), macht Repo aufgebläht. Untracked-Approach besser.

## Konsequenzen

### Positiv
- Neue stdio-MCPs sind ein `npm install` + DB-Zuweisung entfernt — kein Docker-Rebuild
- Boss kann MCPs via bestehenden Approval-Flow installieren lassen — kein neuer Security-Pfad
- Smoke-Test fängt kaputte MCPs ab bevor sie Agents erreichen
- Audit-Trail via Phase-1 `install_log` gilt auch für MCPs

### Negativ
- Einmaliger Docker-Agent-Restart nötig für den Mount — Koordination mit dem Operator
- Host-seitiger `npm install` erfordert Node auf Host (ist vorhanden auf Mac Mini)
- stdio-MCPs mit nicht-standard Runtime (Swift, Ruby, native libs) gehen nicht via Mount — Phase 3 (Gateway)
- `~/.openclaw/mcp-servers/` ist untracked im Git — Disaster-Recovery braucht zusätzliches Backup

## Referenzen

- Betroffene Dateien: `backend/app/services/mcp_registry.py`, `backend/app/services/mcp_sync.py`, `backend/app/models/agent.py`, `backend/alembic/versions/0076_agent_mcp_servers.py`
- Commits: 89d6921 → bdc5811 on branch `feature/boss-install-mcp-phase2`
- Verwandte ADRs: ADR-015 (Phase 1 Install-Approval-Flow)
- Spec: `docs/superpowers/specs/2026-04-18-boss-installation-system-design.md` §5
- Plan: `docs/superpowers/plans/2026-04-18-boss-install-system-phase2-mcp.md`
- Migration: `0076_agent_mcp_servers.py`
