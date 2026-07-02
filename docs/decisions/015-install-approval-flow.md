# ADR-015 — Install-Approval Flow for Boss

**Status:** Accepted
**Datum:** 2026-04-18
**Scope:** Backend/Dispatch | Backend/DB | Frontend/State

## Kontext

Boss (claude-code CLI, host-based Orchestrator per ADR-014) stellt regelmässig fest, dass Docker-Worker-Agents fehlende Capabilities haben — ein Research-Agent ohne `web-performance` Skill, ein Dev-Agent ohne spezielles Plugin. Bisher sind alle Install-Endpoints User-Auth-only: Boss kann keine Installation initiieren, sondern nur den Operator per Chat/Discord anpingen. Das ist ein Flaschenhals der Bosss Autonomie einschränkt.

## Entscheidung

Neuer agent-scoped Endpoint `POST /api/v1/agent/install-requests` erzeugt Approval-Rows mit `action_type in {install_skill, uninstall_skill, install_plugin, uninstall_plugin}` (MCP-Support folgt in Phase 2). Nach dem Approve des Operators im Inbox ruft der `InstallExecutor` die **Service-Layer-Funktionen direkt** auf — keine User-Auth-Bypass-Hacks, keine privilegierten Tokens an Boss.

### Kernentscheidungen

1. **Idempotenz** via Duplicate-Detection auf `(action_type, target_agent_id, name)` — gleicher Request 2× gibt dieselbe `approval_id` zurück (HTTP 200 statt 201).
2. **Pre-Check**: bereits installiert → HTTP 409, keine Approval-Noise in der Inbox des Operators.
3. **7-Tage-TTL** auf Approvals via `expires_at` — alte Requests werden `expired`.
4. **Service-Layer-Direct** im Executor — `_call_skill_install()` macht git-clone, `_call_plugin_install()` ruft `_bridge_post` (cli-bridge) via Thread-Executor. Keine internen HTTP-Calls.
5. **Auto-Rollback** bei Install-Fehler via `previous_state` in `install_log`.
6. **Allowlist** als Regex im Code (`backend/app/services/install_allowlist.py`) — Boss kann keine neuen Quellen whitelisten. Erlaubt: GitHub `anthropic/obra/getcursor` für Skills, `claude-plugins-official` + `github:claude-plugins/*` + `github:anthropic/*` für Plugins, `@modelcontextprotocol/*` + `@supabase/*` + `@vercel/*` + `@cloudflare/*` NPM-Scopes für MCP (Phase 2).
7. **Always through Inbox** — kein Auto-Approve, auch nicht für L1 (vom Operator explizit so entschieden 2026-04-18).
8. **Scope-Check via `get_agent_effective_scopes()`** — respektiert MC's Backward-Compat (scopes=[] = ALL_SCOPES). Boss hat scopes=[] → effektiv agents:manage.
9. **Uninstall erlaubt** — Boss darf auch `uninstall_*` Requests stellen (vom Operator explizit so entschieden).
10. **Sync-Lock** per Agent: Redis-Key `mc:agent:{id}:install_lock` TTL 60s vor sync-config, verhindert Race mit manuellen sync-config-Calls.

## Alternativen

- **Ein User-Token für Boss**: Auth-Hole, man wüsste nie "war das der Operator oder Boss?" → Verworfen.
- **HTTP-Calls innerhalb Executor**: bräuchte einen privilegierten Service-Token, komplex zu verwalten, mehr Oberfläche für Fehler → Verworfen.
- **Auto-Approve für L1**: der Operator will volle Kontrolle — auch kleine Änderungen durch Inbox → Verworfen.
- **MCP-Gateway-Proxy in Phase 1**: YAGNI — `mc-agent-base` hat Python + Node, 95% der stdio-MCPs laufen schon via Mount. Deferred bis konkreter Bedarf.

## Konsequenzen

### Positiv
- Boss kann Skills/Plugins für beliebige Agents installieren lassen — full self-service nach Operator-OK.
- Jeder Install/Uninstall ist audit-trailed in `install_log` mit Rollback-Pointer.
- Neue Install-Types sind additive (nur Executor-Handler + Allowlist-Pattern ergänzen).
- Request-Duplikate blocken nicht — idempotenter Endpoint macht Retry-Fehler vergebend.

### Negativ
- Allowlist-Erweiterung braucht Code-Change + Deploy. Bewusst so gewählt — der Operator bleibt der Gatekeeper für neue Trust-Quellen.
- Boss kann kein Runtime-Debugging an fremden Configs machen — Installation ist der einzige Write-Pfad zu anderen Agents (gut für Audit, restriktiver als freie Config-Edits).
- openclaw-Agent-Sync (Henry, Boss selbst) ist noch no-op im Executor (RPC-Singleton nur im Router-Layer zugänglich) — Phase 2 kann das via Refactoring adressieren.

## Referenzen

- Betroffene Dateien: `backend/app/routers/agent_scoped.py`, `backend/app/services/install_executor.py`, `backend/app/services/install_allowlist.py`, `frontend-v2/src/components/shared/InstallRequestCard.tsx`
- Spec: `docs/superpowers/specs/2026-04-18-boss-installation-system-design.md`
- Plan: `docs/superpowers/plans/2026-04-18-boss-install-system-phase1.md`
- Migration: `0075_install_log_and_approval_failure_reason.py`
- Commits: 434a70d → 5703d03 on branch `feature/boss-install-system`
- Verwandte ADRs: ADR-009 (agent-scoped router), ADR-010 (PBKDF2 auth), ADR-014 (Boss host-runtime)
