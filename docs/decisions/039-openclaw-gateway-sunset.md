# ADR-039 — OpenClaw Gateway Sunset

**Status:** Accepted
**Datum:** 2026-05-16 (Proposed) · 2026-05-17 (Accepted)
**Scope:** Infra/Runtime, Backend/Dispatch, Backend/DB, Frontend/State

## Kontext

Mission Control wurde ursprünglich auf den OpenClaw Gateway aufgebaut (Port 18789, WebSocket-RPC). Über die letzten 6 Wochen hat eine schleichende Migration ~90% der Funktionalität auf direkte Pfade verlagert:

- **ADR-011 (2026-04-08):** Docker-Agents pollen direkt via HTTP, kein Gateway-RPC.
- **ADR-013 (2026-04-08):** Docker V2 live; Settings via `cli-bridge.py`, nicht Gateway-RPC.
- **ADR-014 (2026-04-17):** Boss läuft als macOS-Host launchd-Job mit nativem `claude`-Binary + Anthropic-OAuth — kein Gateway.
- **ADR-019 (2026-04-20):** 9 Docker-Agents wechseln auf `claude`-Binary in `mc-claude-agent:latest` Image. Kein Gateway.
- **ADR-027/028 (2026-04-28/29):** Runtime-Binding über `agents.runtime_id` (DB). Gateway orthogonal.
- **ADR-029 (2026-04-30):** Hermes mit eigener `hermes-bridge.py` — kein Gateway.

Stand 2026-05-16 hängt nur noch **ein** Agent am Gateway: **Henry** (Board Lead, `agent_runtime="openclaw"`). Henry hat historisch die "Front Door"-Rolle gespielt — als Mensch-Interface zum Orchestrator. Boss ist defacto schon der echte Orchestrator (ADR-014). Backend hält dennoch ~2700 LOC Gateway-Code in Bereitschaft:

- `services/openclaw_rpc.py` (798 LOC) — WebSocket-RPC-Client
- `services/gateway_sync.py` (425 LOC) — Startup-Sync von Gateway-Agents in DB
- `services/provisioning.py` (384 LOC) — Pushed SOUL.md/TOOLS.md via `agents.files.set`
- `services/gateway_secrets_sync.py` (168 LOC) — Verteilt LLM-API-Keys in `openclaw.json`
- `routers/gateway.py` (~300 LOC) — Health, Sessions, Discord-Channels, Provider-Status
- 35+ RPC-Call-Sites in Dispatch, Watchdog, Task-Runner, Approvals, Chat, Research

Frontend exponiert Gateway-Konzepte an drei Stellen: `/skills` (Gateway-Health + Skill-Marketplace), `/settings` (OpenClaw Provider Status + Sync-Button), `/agents/[id]` (Provision/Sync/Reset Buttons). DB hält die `Gateway`-Tabelle plus `agents.gateway_id`, `agents.gateway_agent_id`, `agents.workspace_path`, `boards.gateway_id`.

Telegram, Discord und alle 11 Worker-Agents sind **nicht** Gateway-abhängig — Telegram nutzt direkt die Bot-API, Discord nutzt Webhooks + Bot-API direkt, alle 11 Agents nutzen poll-based HTTP oder direkte Docker/Host-Runtimes.

## Entscheidung

Die OpenClaw-Gateway-Komponente wird vollständig aus Mission Control entfernt — als Runtime, als Code-Pfad, als DB-Schema, als Frontend-Konzept, als Host-Service. Henry wird nicht auf eine andere Runtime migriert, sondern aus dem System gelöscht. Boss übernimmt die Board-Lead-Rolle für `dispatch.find_dispatch_target()` vollständig.

Umgesetzt in 4 Phasen (Milestone v0.9):

- **Phase 28:** Henry-Sunset + Boss-Promotion (Active-Task-Migration, Henry-DB-Delete, Dispatch-Default).
- **Phase 29:** Backend-Code-Removal (openclaw_rpc, gateway_sync, gateway_secrets_sync, provisioning Gateway-Pfad, routers/gateway entfernt; Discord-Bot-Endpoints in eigenen Router; sessions_list-basierte Watchdog-Logik durch DB+Redis ersetzt).
- **Phase 30:** DB-Schema-Cleanup (Drop `Gateway`-Tabelle, `agents.gateway_*`, `boards.gateway_id`; neue Tabelle `discord_config` für globale Discord-Konfig; `agent_runtime` Enum entfernt "openclaw").
- **Phase 31:** Frontend-Rebuild + Host-Cleanup (/skills neu in 3 Tabs ohne Gateway; /settings Provider-Status raus; openclaw launchd entfernen; ~/.openclaw/ Gateway-Dirs archivieren + löschen, Symlinks zu ~/.mc/ behalten).

## Alternativen

- **Alternative A: Henry zu cli-bridge Docker (mc-claude-agent) migrieren.** Henry hätte als 11. Docker-Agent weitergelebt — gleicher Pattern wie Rex/Cody/Davinci. **Verworfen weil:** Henry ist defacto redundant zu Boss (beide Orchestrator-Rollen). Eine Migration würde nur die Komplexität in einer neuen Runtime weiterleben lassen, ohne funktionalen Gewinn. Der Operator hat klargestellt: "Henry brauchen wir garnicht mehr — Boss ist bereits der Orchestrator, Henry war nur Front Door."

- **Alternative B: Henry zu Host-launchd (Pattern von Boss) migrieren.** Mit nativem `claude`-Binary, eigener tmux-Session, ttyd-Web-Terminal. **Verworfen weil:** zusätzlicher Host-Service ohne klares Use-Case. Pattern existiert für Boss + Hermes — drittes Mal lohnt sich nicht. Setup-Aufwand (ttyd, plist, ports) vs. Null-Gewinn.

- **Alternative C: OpenClaw Gateway als Standalone-Tool für ad-hoc CLI-Sessions beibehalten (Mac Mini :18789 läuft passiv weiter), MC kappt nur die RPC-Calls.** **Verworfen weil:** der Operator möchte Clean Cut. Maintenance-Overhead für einen Prozess der niemanden mehr serviert. ~/.openclaw/ würde verwaist bleiben.

- **Alternative D: Strangler Pattern — `openclaw_rpc.py` zu No-Op-Stub, dann graduelles Cleanup.** **Verworfen weil:** verlängert den "halben Zustand" und produziert toten Code in main. Der Operator wählte den GSD-Phasen-Ansatz mit klaren atomaren Schritten.

## Konsequenzen

### Positiv

- **~2700 LOC Code-Reduktion** im Backend (services/openclaw_rpc.py, gateway_sync.py, provisioning.py, gateway_secrets_sync.py, routers/gateway.py + 35+ Call-Sites).
- **Architektur klarer:** Single Source of Truth für Agent-Runtimes ist DB (`agents.runtime_id`). Kein zweiter Sync-Pfad mehr.
- **Provisioning vereinfacht:** nur noch `cli-bridge.py`-basiert (Docker-Agents) + Host-launchd (Boss/Hermes). Kein RPC-config-Patch mehr.
- **Frontend übersichtlicher:** /skills wird zu klaren 3-Tab-Layout (Local Skills / CLI Plugins / MCP Servers). Keine zwei Wahrheiten "Gateway-Skills vs lokale Skills".
- **Filesystem-Layout aufgeräumt:** ~/.openclaw/ enthält nur noch Symlinks zu ~/.mc/. Gateway-eigene Dirs weg.
- **Mac Mini Port 18789 frei:** keine WebSocket-Daemon mehr im launchctl.
- **Onboarding einfacher:** Neue Beitragende müssen das Gateway-Konzept nicht mehr verstehen.

### Negativ

- **Verlust der Henry-Persönlichkeit:** Die als "Henry" konfigurierte SOUL.md geht (außer der Operator übernimmt Aspekte in Boss). Acceptable, da Boss schon defacto Orchestrator ist.
- **Migration-Risiko bei Phase 28:** Active-Tasks an Henry müssen sauber umgeleitet werden. Mitigiert durch Pre-Flight-Check + Dry-Run.
- **DB-Migration ist nicht trivial reversibel.** Pre-Migration Backup über `./backup.sh` Pflicht. Down-Migration getestet aber nie 100% verlustfrei wenn Daten gelöscht wurden.
- **Frontend-Breakage zwischen Phase 29 und Phase 31:** Während dieser Zeit zeigen /skills + /settings (Provider-Status) 404/503. Akzeptabel weil Phase 31 sofort nachzieht.
- **Wir können das nicht zurückrollen:** Gateway-Binary + ~/.openclaw/ Files werden gelöscht. Wenn wir doch wieder ein Gateway brauchen würden: Neuinstallation + Re-Pairing nötig.

### Wo künftig aufpassen

- **Diskussion mit External Tools:** Wenn der Operator künftig auf openclaw-eigene CLI-Tools angewiesen ist (z.B. für ad-hoc Tasks außerhalb von MC): einen separaten Pfad bauen, nicht das alte Gateway-Setup reanimieren.
- **Discord-Bot-Token:** Wandert in eigene Tabelle `discord_config`. Beim Konfig-Reset darauf achten dass Bot weiterhin verfügbar bleibt.
- **CLI-Plugin-Management:** Bislang teilte sich der Mechanismus mit Gateway-Skills die "Skills"-Konzepte. Nach Sunset sind Skills nur noch lokale Markdown-Files + CLI-Plugins (separate Cache). Klare Trennung.
- **Test-Coverage:** Vor Phase 28 sicherstellen dass Boss-Dispatch-Flow Tests vollständig sind. Wenn Boss versagt: niemand übernimmt.

## Referenzen

- **Milestone Spec:** `.planning/milestones/v0.9-ROADMAP.md`
- **Design Spec:** `docs/superpowers/specs/2026-05-16-openclaw-gateway-sunset-design.md`
- **Memory Decision:** `~/.claude/projects/<project-slug>/memory/project_openclaw_henry_removal_decision_2026-05-16.md`
- **Verwandte ADRs:**
  - ADR-003 (Triple-Runtime-Architektur) — Ursprung
  - ADR-011 (HTTP-Polling für Docker-Agents) — erster Gateway-Umweg
  - ADR-014 (Boss Host Runtime) — Orchestrator-Migration
  - ADR-019 (Claude Fleet Hybrid) — 9-Agent-Migration weg von Gateway
  - ADR-027 (Universal Agent ↔ Runtime Binding) — DB als SoT
  - ADR-028 (Runtime Registry DB-only) — Registry-Wahrheit
  - ADR-029 (Hermes-Bridge Host-Worker) — neues Pattern ohne Gateway
- **Betroffene Dateien (Removal-Targets):**
  - `backend/app/services/openclaw_rpc.py` (798 LOC) — komplett raus
  - `backend/app/services/gateway_sync.py` (425 LOC) — komplett raus
  - `backend/app/services/provisioning.py:_provision_agent_background` (Gateway-Pfad) — Pfad raus, cli-bridge bleibt
  - `backend/app/services/gateway_secrets_sync.py` (168 LOC) — komplett raus
  - `backend/app/routers/gateway.py` (~300 LOC) — komplett raus; Discord-Bot-Endpoints wandern zu neuem `routers/discord.py`
  - `backend/app/services/dispatch.py` — `chat_send`/`chat_send_isolated` Pfade entfernen
  - `backend/app/services/dispatch_delivery.py:199,219` — RPC-Calls raus
  - `backend/app/services/task_lifecycle.py` — RPC-Calls raus
  - `backend/app/services/task_runner.py:770` — Eskalations-RPC raus
  - `backend/app/services/watchdog/{session,task}_monitor.py` (10+ Call-Sites) — sessions_list + chat_send raus
  - `backend/app/services/cost_collector.py:47` — sessions_list raus
  - `backend/app/services/meeting_service.py` — RPC raus
  - `backend/app/services/telegram_bot.py` — RPC-Pfad (telegram:* Sessions) raus, direkte HTTP bleibt
  - `backend/app/routers/{agents,agent_scoped,approvals,chat,research,skills,models,tasks}.py` — RPC-Imports + Calls raus
  - `backend/app/main.py` — `_deferred_gateway_sync` Lifespan-Hook raus
  - `backend/app/config.py` — `gateway_url`, `openclaw_ws_url`, `openclaw_token` raus
  - `backend/app/models/{agent,board}.py` — Spalten + Gateway-Klasse raus
  - `backend/alembic/versions/01XX_drop_gateway_schema.py` — neu
  - `frontend-v2/src/lib/{api,types}.ts` — Gateway/OpenClaw-Interfaces raus
  - `frontend-v2/src/app/{skills,settings,agents/[id]}/page.tsx` — Gateway-Code raus
  - Host: `~/Library/LaunchAgents/com.openclaw.gateway.plist` (oder ähnlich) — unload + delete
  - Host: `~/.openclaw/{identity,openclaw.json,logs,cron,credentials,extensions,plugin-store,delivery-queue,exec-approvals.json,restart-sentinel.json}` — archivieren + löschen
- **Commits:** TBD (entsteht in Phase 28–31)

## Outcome (Phase 31 close, 2026-05-17)

Alle vier v0.9-Phasen geliefert:

- **Phase 28 (Henry-Sunset)** — Henry-Agent aus DB gelöscht; Boss übernimmt Board-Lead via `is_board_lead`-Flag. Active-Task-Migration via `mc_henry_sunset.py` Skript mit Dry-Run-Modus + Discord-OPS-Bericht. FK-swap Migration 0121 (`task_comments.author_agent_id` → ON DELETE SET NULL) + Main-Migration 0122 (Pre-Flight + reassign tasks → Boss + promote Boss + delete Henry). `dispatch.find_dispatch_target()` defaultet jetzt auf Boss. Telegram + Discord Smoketests grün.

- **Phase 29 (Backend-Detangling)** — ~2700 LOC Backend-Code raus über 10 Plans / 4 Waves. **Gelöscht:** `services/openclaw_rpc.py`, `services/gateway_sync.py`, `services/gateway_secrets_sync.py`, `services/telegram.py` (Gateway-Pfad), `services/gateway_client.py`, `routers/gateway.py`. **Discord-Bot-Endpoints** leben in eigenem `routers/discord.py` (Plan 29-01). **Refactored:** ~200 Call-Sites in `dispatch.py`, `dispatch_delivery.py`, `task_lifecycle.py`, `task_runner.py`, `watchdog/*`, `routers/agents.py`, `agent_scoped.py`, `tasks.py`, `skills.py`, `models.py`, `research.py`, `content.py`, `chat.py`, `approvals.py`, `system.py`, `operations.py`, `cost_collector.py`, `meeting_service.py`, `telegram_bot.py`, `workflow_service.py`, `provisioning.py`. `OPENCLAW_WS_URL` + `OPENCLAW_TOKEN` aus `config.py` raus. `watchdog/task_runner` nutzt DB+Redis statt `sessions_list()` für Stale-Detection. Pytest grün, Telegram + Discord live verifiziert.

- **Phase 30 (DB-Cleanup)** — Alembic 0123 (`drop_gateway_schema_add_discord_config`) droppt `gateways`-Tabelle + `agents.gateway_id` + `agents.gateway_agent_id` + `boards.gateway_id`. **`agents.workspace_path` BLEIBT** — Phase 14 / ADR-022 Repurpose auf agent-home-path (`~/.mc/workspaces/<slug>`), NICHT Gateway-VPS-Path wie SQLModel-Docstring fälschlicherweise behauptete. Neue Tabelle `discord_config` (single-row, guild_id/category_id/bot_configured) mit CHECK-Constraint + Daten-Migration aus `gateways.discord_*`. `agent_runtime`-Enum entfernt `"openclaw"`-Value. SQLModel-Klassen `Gateway`, `gateway_id`-Felder aus `backend/app/models/` entfernt. Up + Down Migration getestet (SQLite E2E + Invariant-Tests).

- **Phase 31 (Frontend + Filesystem)** — **`/skills` Page** komplett neu in 3 Tabs (Local Skills + CLI Plugins + MCP Servers), keine Gateway-Health-Anzeige (31-01). **`/settings`** OpenClaw-Provider-Block + "Sync to Gateway"-Button raus (31-02). **`/agents/[id]`** Provision/Reset/Sync-Buttons strikt auf `agent_runtime === 'cli-bridge'` gegated (31-03). **`lib/types.ts` + `lib/api.ts`** Gateway-Interfaces (`Gateway`, `OpenClawHealth`, `OpenClawModel`, `OpenClawSyncResult`, `GatewaySession`) + `api.gateways.*` / `api.openclaw.*` / `api.secrets.syncToGateway()` entfernt. Workflows migriert auf `api.discord.*` (31-04). **Erhalten geblieben:** `GatewayMessage` / `GatewayMessagePart` Types in `types.ts` (Anthropic chat-history shapes — historisch fehlbenannt, Rename in Follow-up). **Filesystem-Cleanup (31-06)** — der Operator führt manuell aus: `~/.openclaw-archive-2026-05-17.tar.gz` Backup, `launchctl unload` openclaw plist, `rm` der Gateway-Dirs (identity, logs, cron, credentials, extensions, plugin-store, delivery-queue, exec-approvals.json, restart-sentinel.json). **`~/.openclaw/{agents,skills,plugins,mcp-servers}` Symlinks zu `~/.mc/`** BLEIBEN (aktiv genutzt von cli-bridge Agents).

Sechs-Wochen-Kriechmigration plus vier-Phasen-Final-Sweep schliessen die OpenClaw-Ära von Mission Control ab. **Multi-Agent-Stack läuft jetzt direkt:** Host-Boss (native `claude` Binary + Anthropic OAuth), 9 Docker-Agents (`mc-claude-agent:latest` Image mit `claude` Binary + `CLAUDE_CODE_OAUTH_TOKEN`), Sparky (`openclaude` mit LM Studio / Ollama Cloud), Hermes (host tmux mit vLLM-Provider Qwen3.6-35B), Jarvis (LiveKit / xAI Grok Voice). Kein WebSocket-RPC, kein Port :18789, keine `~/.openclaw/identity` Keys mehr im aktiven Pfad.

Plan-Trace: 28-01..03, 29-01..10, 30-01..03, 31-01..06.
