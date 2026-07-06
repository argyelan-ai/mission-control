# Mission Control — Architektur

> **Lebende Dokumentation.** Bei jeder Architektur-Änderung (neue Services, Runtime-Wechsel, Dispatch-Flow, Schema-Migration) muss dieses Dokument angepasst werden. Bei Design-Entscheidungen zusätzlich neues ADR in `docs/decisions/` anlegen.

**Letztes Update:** 2026-05-17
**Stand:** v0.9 OpenClaw Gateway Sunset complete (Phases 28-31, ADR-039 Accepted)

---

## Übersicht

Mission Control (MC) ist ein selbst-gehostetes AI Agent Command Center. Es orchestriert mehrere AI-Agents (Claude Code, openclaude, custom), verteilt Tasks via strukturierte Dispatch-Messages, überwacht deren Ausführung und aggregiert Learnings. Ziel: der Operator kann Ideen beschreiben, das Agent-Team setzt sie um — koordiniert durch Henry (Board Lead) und unterstützt durch spezialisierte Worker.

**Zentrale Eigenschaften:**
- **Selbst-gehostet**: Läuft vollständig lokal auf Mac Mini M4 (Docker Compose)
- **Multi-Runtime**: 2 parallele Agent-Runtimes (Host launchd + Docker cli-bridge) — OpenClaw Gateway entfernt (v0.9, ADR-039)
- **Strukturierte Dispatch-Messages**: Agents bekommen vollständigen Task-Kontext inkl. Curl-Callbacks
- **Dispatch ACK Handshake**: Task bleibt `inbox` bis Agent explizit bestätigt
- **Watchdog-getriebene Recovery**: Phase-Completion, ACK-Timeout, Stale-Progress, Silent-Abort-Auto-Block (ADR-046)
- **Single Source of Truth**: DB → Jinja2-Templates → gerenderte Dateien (settings.json, SOUL.md, worker.sh)

---

## Stack-Übersicht

```
Browser (Caddy :80) → Frontend (Next.js 15, :3000)
                     → Backend (FastAPI, :8000)
                        ↓
                     PostgreSQL 16 (:5432) + Redis 7 (:6379) + Qdrant (:6333)
                        ↓
               ┌────────┴────────┐
               ↓                 ↓
     Host launchd Agents    Docker Agent Container (mc-agent-*)
     (Boss, Hermes)         (9 Container, claude/openclaude + tmux + poll.sh)
     tmux + native binary   HTTP-Poll /api/v1/agent/me/next-task
     HTTP-Poll
```

**Backend-Volumes (kritisch):**
- `${HOME}/.openclaw` — Agent-Workspaces, settings.json, Tokens
- `/var/run/docker.sock` — Docker-Zugriff für Agent-Container Lifecycle

---

## Bausteine

### 1. Backend (FastAPI)

**Pfad:** `backend/app/`

**Routers (21)**, gruppiert nach Domäne:

| Gruppe | Router | Zweck |
|---|---|---|
| Auth | `auth.py` | User JWT, Agent PBKDF2, Legacy Token |
| Agents | `agents.py`, `agent_scoped.py`, `agent_templates.py` | Agent CRUD, Provisioning, Agent-seitige Callbacks (Status-Updates) |
| Tasks | `tasks.py`, `consensus.py` | Task CRUD, Multi-Agent Konsens |
| Boards & Projects | `boards.py`, `projects.py`, `project_git.py` | Board/Project CRUD, GitHub-Sync |
| Memory & Intelligence | `memory.py`, `system.py` | Knowledge Base, 3-Layer Memory (Qdrant), Insights |
| Realtime | `activity.py`, `cli_terminal.py` | SSE Streams, PTY WebSocket |
| Discord | `discord.py` | Per-agent Channel CRUD, Bot-Config (post-Gateway-Sunset, v0.9) |
| Ops | `approvals.py`, `runtimes.py`, `workflows.py`, `scheduler` | Approvals, Runtime-Mgmt, Automation |
| Admin | `credentials.py`, `secrets.py`, `cli_plugins.py`, `skills.py` | Credentials Vault, Plugins, Tags |

**Services (29)** — Singletons, alle async:

| Service | Zweck | Interval |
|---|---|---|
| `dispatch.py` | Task → Agent zuweisen, Structured Message bauen, RPC-Send | on-demand |
| `task_runner.py` | Dispatch-ACK-Timeout, Stale Progress, Circuit Breaker, Silent-Abort-Auto-Block (ADR-046, cli-bridge v1) | 60s |
| `watchdog/` (core + mixins) | Phase-Completion, Session-Recovery, Health-Checks | 30s |
| `intelligence.py` | Task-Duration-Analyse, Failure Patterns, LLM-Destillation (Ollama) | 300s |
| `git_service.py` | GitHub Repo+PR Management für Agents | on-demand |
| `provisioning.py` | Agent-Create Background-Task, Template-Render (cli-bridge only seit v0.9) | on-demand |
| `template_renderer.py` | Jinja2-Render: SOUL.md, TOOLS.md, HEARTBEAT.md, settings.json | on-demand |
| `activity.py` + `sse.py` | ActivityEvent Emit + Redis pub/sub Fan-Out | streaming |
| `dispatch_delivery.py` | Task → Agent Delivery (cli-bridge filesystem-queue, host tmux-paste) — Single Path post-v0.9 | on-demand |
| `plugin_manager.py` | CLI-Plugin Shared-Cache, per-Agent Allowlist | on-demand |
| `memory_indexing.py` | Auto-Embedding + Qdrant-Upsert bei Memory-Create | on-demand |
| `memory_query.py` | Hybrid Vector/Keyword Search ueber 3 Qdrant-Collections | on-demand |
| `embedding_service.py` | nomic-embed-text-v1.5 via Spark (LM Studio, 768-dim) | on-demand |
| `qdrant_service.py` | 3 Collections (semantic/agent/episodic), Recency-Boost | on-demand |
| `auto_memory.py` | Auto-Journal bei Task-Done, Auto-Lesson bei Failure, Weekly Digest | event-driven |
| `install_executor.py` | Skills-/Plugin-Install & -Uninstall nach Operator-Approval (ADR-015). Service-Layer-direct, Audit-Trail in `install_log`, Auto-Rollback | on-demand |
| `mcp_registry.py` | Filesystem-basierte Registry für installierbare MCP-Server unter `~/.openclaw/mcp-servers/<name>/manifest.json`. CRUD + npm/git-install + JSON-RPC Smoke-Test. Siehe ADR-016 | on-demand |
| `mcp_sync.py` | Rendert `.mcp.json` pro Agent aus Registry + `Agent.mcp_servers` Allowlist. Schreibt nach `~/.openclaw/agents/<slug>/claude-config/.mcp.json`. Wird von `sync-config`-Flow und `InstallExecutor` aufgerufen | on-demand |

**DB (29 SQLModel Tables)** — Highlights:

- **Agent** — 29 Felder, inkl. `agent_runtime` (`cli-bridge` / `host` — `openclaw`-Value entfernt in v0.9 Migration 0123), `provision_status`, `current_task_id` (FK mit `use_alter=True` für Zyklus-Break), `scopes` (16 Permissions), `cli_plugins`, `skill_filter`. **`workspace_path`** bleibt — Phase 14 / ADR-022 Repurpose auf `~/.mc/workspaces/<slug>`, NICHT Gateway-VPS-Path.
- **Task** — 100+ Felder, inkl. `parent_task_id` (Phasen), `callback_agent_id` (Done-Notification), `dispatched_at` + `ack_at` (ACK-Handshake), `workspace_path`, `dispatch_attempt_id`
- **Board** — Workflow-Rules (`require_review_before_done`, `require_approval_for_done`), `default_project_id`, `stats_cache` (1h TTL)
- **Project** — `github_repo_url`, `phases` (ProjectPhase), Status-Flow
- **BoardMemory** — Triple-Scoping: `board_id` set = Board Memory, `agent_id` set = Agent Knowledge, beide null = Global Knowledge. Typen: knowledge/decision/lesson/reference/journal/concept/weekly_review/research/insight. Auto-indexiert in Qdrant (3 Layer: semantic/agent/episodic)
- **Approval** — Circuit Breaker Events, Expiry
- **Credential** — Fernet-verschlüsselt

**Migrations:** 75 total (0001 → 0075). Neueste: 0073 host runtime, 0074 install_requests table, 0075 install_log + approval failure_reason.

**Auth-System (3-Tier):**
1. **User JWT (HS256)** — Login via `auth/login`, localStorage-Storage, `token_version` für Logout-Invalidierung
2. **Agent PBKDF2 (200k iter)** — `POST /agents` erzeugt Token einmalig, Redis-Cache `SHA256(token) → agent_id` (5min TTL) verhindert N×200ms Hash pro Request
3. **Legacy LOCAL_AUTH_TOKEN** — nur falls explizit gesetzt, Fallback

**16 Scopes**, default-gefiltert pro Agent-Typ (Lead/Developer/Planner/Reviewer/Deployer/Researcher/Writer). `require_scope()` Dependency erzwingt Scopes backend-seitig, zusätzlich werden TOOLS.md-Sektionen gefiltert (weniger Tokens, weniger Angriffsfläche).

### 2. Frontend (Next.js 15)

**Pfad:** `frontend-v2/src/`

**Pages (App Router):**

| Seite | Zweck |
|---|---|
| `/` Home | Horizontale Pipeline (Inbox/In Progress/Review/Done), Aktivitäts-Feed |
| `/tasks` | Projects-View (Linear-Style), Phasen-Hierarchie, Ad-hoc |
| `/inbox` | Approval/Review Queue, Task-Acceptance |
| `/agents` + `/agents/[id]` | Agent-Grid, Detail mit 4 Tabs (overview/skills/config/memory) |
| `/sessions` | Live PTY Terminal zu Docker-Agents, Lifecycle-Buttons (Start/Stop/Restart) |
| `/chat` | Gateway Chat, direkte Agent-Kommunikation |
| `/memory` | 3-Layer Memory (Episodic/Semantic/Agent Tabs), Qdrant-Suche, Scope-Dropdown |
| `/insights` | Intelligence Dashboard: KPIs, Agent-Performance, Failure Patterns, LLM Reports |
| `/runtimes` | LM Studio / Ollama Runtime-Verwaltung |
| `/schedule` | Cron Jobs + Runs |
| `/workflows` | Workflow-Builder (YAML) + Execution Logs |
| `/settings` | Profile, Autonomy, Intelligence, Secrets, Admin Users, CLI Plugins (7 Tabs) |
| `/skills` | Skill-Marketplace, Team-Zuweisungen (Matrix), Plugin Audit Trail |
| `/content` | Content-Pipeline (Research → Review → Publish) |

**State:**
- **TanStack Query v5** für Server-State. Polling-Intervalle: Inbox/Tasks 15s, Chat 5-10s, Insights 30-120s, Memory 60s. `staleTime: 5s`, `gcTime: 5min`, `refetchOnWindowFocus: true`
- **Zustand** (`lib/store.ts`) für UI-State (activeBoardId, sidebarCollapsed, commandPaletteOpen) — persistiert in localStorage
- **SSE Streams** (`lib/sse.ts`) für Realtime-Events (`useAgentStream`, `useActivityStream`, `useApprovalStream`). Token-Auth via Query-Param (EventSource kann keine Header)

**API Client** (`lib/api.ts`): typed, mit `request<T>()` Wrapper, Auto-401 → /login, JWT aus localStorage.

**Design System:**
- **Dark Mode only**, Tailwind v4 `@theme`-Tokens in `globals.css`
- **Glasmorphism** (GlassCard): `backdrop-blur-[16px]`, `bg-[rgba(255,255,255,0.03)]`, Top-Edge-Highlight
- **Geist Sans/Mono** via `next/font`
- **Farben:** bg-base #0A0A0A → bg-elevated #1A1A1A; online #00CC88, warning #F59E0B, error #EF4444, accent #8B5CF6
- **Framer Motion** für Animationen, **Ambient Static Blobs** statt Animated Backgrounds (Performance)

**Component-Organisation nach Ownership:**
- `components/layout/` — AppShell, Sidebar, StatusBar
- `components/shared/` — GlassCard, KPICard, StatusDot, CommandPalette
- `components/task/` — TaskDetailPanel, TaskHeader, TaskActions, TaskComments, TaskHistory
- `components/agent/` — AgentCard, AgentGrid, CliTerminalTab
- `components/memory/` — MemoryLayerTabs, EpisodicTimeline, SemanticCardGrid, AgentLessonMatrix, MemoryQueryBar

**xterm.js Terminal** (`app/sessions/page.tsx`): WebSocket → `/api/v1/agents/{id}/terminal?token=...` → PTY-Proxy im Backend → `docker exec -itu agent tmux attach`. Scrollback 5000, copy-on-select, Cmd+V paste, Auto-Reconnect nach 3s. Lifecycle-Buttons: Start/Stop/Restart.

### 3. Docker Stack

**Main Stack** (`docker-compose.yml`):

| Service | Image | Volumes | Zweck |
|---|---|---|---|
| db | postgres:16-alpine | `mc_postgres_data` | Haupt-DB |
| redis | redis:7-alpine | `mc_redis_data` | Cache, Pub/Sub, Queues |
| backend | Custom (./backend) | `${HOME}/.openclaw`, `/var/run/docker.sock` | FastAPI |
| frontend | Custom (./frontend-v2) | — | Next.js |
| qdrant | qdrant/qdrant | `mc_qdrant_data` | Vector DB (KB/RAG) |
| caddy | caddy:2-alpine | caddy_data | Reverse Proxy :80/:443 |

**Netzwerk:** `mission-control_default` (intern, wird von Agent-Containern als `external: true` mit verwendet).

**Agent Stack** (`docker/docker-compose.agents.yml`) — 10 Container basierend auf `mc-agent-base` Image:

- boss, planner, researcher, shakespeare, rex, davinci (base tools)
- freecode, sparky, tester (+ `gh` CLI)
- deployer (+ `gh` CLI + `vercel` CLI)

**`mc-agent-base` Image** (`docker/mc-agent-base/Dockerfile`):
- `node:22.11.0-alpine`
- Tools: git, curl, python3, bash, openssh-client, tmux
- `openclaude@0.1.8` global installiert
- Non-root `agent` user, `/bin/sh` login shell (wichtig: nicht `/sbin/nologin`, sonst tmux-Zombie)
- `CLAUDE_CONFIG_DIR=/home/agent/.claude/`
- Default: `OPENAI_BASE_URL=https://ollama.com/v1`, `OPENAI_MODEL=glm-5.1:cloud`
- Sparky-Ausnahme: `http://192.0.2.10:1234/v1` (DGX Spark lokal)

**Entrypoint** (`entrypoint.sh`, PID 1):
1. Schreibt `.tmux.conf` (mouse off, history-limit 50000, aggressive-resize)
2. Window 0: `tmux new-session -d -s $AGENT_NAME "openclaude --dangerously-skip-permissions"`
3. Window 1: `poll.sh` im Hintergrund
4. `exec sleep infinity` (PID 1 Keep-Alive, kein CPU-Spin)

**poll.sh**: HTTP-Poll-Loop alle 5s zu `$MC_API_URL/api/v1/agent/me/next-task`, Heartbeat alle 30s. Bei Task: `tmux load-buffer` + `paste-buffer -t {agent}:0` + `Enter` — sendet Prompt an interaktives openclaude in Window 0. Completion-Detection via Output-Stability (6×5s ohne Änderung = fertig).

**Volume-Mount pro Agent:** `${HOME}/.openclaw/agents/{slug}/claude-config:/home/agent/.claude`. `OPENAI_API_KEY=${OLLAMA_API_KEY}` (aus `docker/.env.agents`, gitignored, aus `~/.openclaw/plugin-store/batcode-plugins.env`).

### 4. Host-Side: CLI-Bridge (post-v0.9 Sunset)

**Hinweis:** Der OpenClaw Gateway (WebSocket-Daemon auf Port `18789`) wurde mit v0.9 (ADR-039) komplett entfernt — als Runtime, Code-Pfad, DB-Schema, Frontend-Konzept und Host-Service. Verbleibende Host-Komponenten:

**CLI-Bridge** (`scripts/cli-bridge.py`) — Host-HTTP-Server auf Port `18792`:
- Rendert Agent-Konfig aus Jinja2-Templates (`backend/templates/*.j2`):
  - `cli_agent_settings.json.j2` → `settings.json` + `claude-config/settings.json` (Kopie, **kein Symlink** — Docker-Kompatibilität)
  - `cli_agent.env.j2` → `agent.env` (MC_AGENT_TOKEN)
  - `cli_agent_worker.sh.j2` → `worker.sh` (Host-Worker, Legacy)
  - `SOUL.md.j2`, `HEARTBEAT.md.j2`, `TOOLS.md`, `MEMORY.md.j2`
- Plugin-Settings aus `cli_plugins` DB-Feld (Allowlist, null = alle, [] = keine)
- `enabledPlugins` immer als **dict** gerendert `{k: True for k in plugins}` — Array-Format schlägt openclaude Schema-Validation fehl
- Agent-Verzeichnis-Struktur: `~/.openclaw/agents/{slug}/{settings.json, agent.env, worker.sh, queue/, claude-config/}`

### 5. Agent-Runtime-Typen

Mission Control unterstützt **3 parallele Runtime-Typen** post-v0.9 Sunset (siehe ADR-003 + ADR-014 + ADR-039):

| Runtime | Wo läuft | Dispatch | Terminal | Status |
|---|---|---|---|---|
| **cli-bridge** (Host) | Host tmux via worker.sh | File-Queue `queue/pending/{id}.json` | Host tmux attach | Legacy, deprecated |
| **cli-bridge** (Docker V2) | Docker-Container mc-agent-{slug} | HTTP-Poll `/agent/me/next-task` + `tmux send-keys` | PTY-Proxy WS → docker exec tmux attach | Produktiv seit 2026-04-08 |
| **host** (Boss) | macOS launchd-Job auf dem Host | HTTP-Poll `/agent/me/poll` + `tmux paste-buffer` | ttyd → WS-Proxy → Browser xterm.js | Produktiv seit 2026-04-17 |
| **host** (Hermes) | macOS launchd-Job + eigene `hermes-bridge.py` | tmux-Session `hermes-worker`, vLLM (Qwen/Qwen3.6-35B-A3B-FP8) | xterm.js via `cli_terminal.py` | Pilot v0.8 (2026-04-30, ADR-029) |

Beide Docker-V2 und Host-cli-bridge setzen `agent_runtime = 'cli-bridge'` in der DB — unterschieden werden sie dadurch ob ein Docker-Container `mc-agent-{slug}` läuft (Check via `docker ps` im `/docker-sessions/agents` Endpoint). Der `"openclaw"`-Enum-Value ist mit Migration 0123 (Phase 30, v0.9) entfernt.

**Alle Runtimes sind poll-based** post-v0.9 — sie pollen aktiv `/agent/me/poll` statt eine Gateway-Session zu halten. Single Source of Truth: `dispatch.find_dispatch_target()` + `dispatch_delivery.deliver()`. Henry (einziger Gateway-Agent) wurde in Phase 28 aus der DB gelöscht, Boss übernimmt die Board-Lead-Rolle via `is_board_lead`-Flag.

#### Runtime: `host` (NEU 2026-04-17)

Boss läuft seit 2026-04-17 als macOS launchd-Job direkt auf dem Host (nicht im
Docker-Container) — siehe ADR-014.
- **Binary:** offizielles Anthropic `claude` (Opus 4.7) mit OAuth-Login (Claude Max Subscription, kein API-Key)
- **Lifecycle:** launchd `com.openclaw.boss` → `~/.openclaw/agents/boss-host/entrypoint.sh` → tmux-Session `boss-host` mit Window 0 (claude) + Window 1 (poll.sh)
- **Terminal-Visibility:** `com.openclaw.boss-ttyd` (ttyd → tmux) auf 127.0.0.1:7681. Backend proxied via `/api/v1/host-agents/{id}/terminal` (WS) zur Sessions-Page
- **DB:** `agents.agent_runtime = 'host'` (Migration 0073)
- **Sync:** `docker_agent_sync.py` skippt `host`-Agents (kein Container-Lifecycle)
- **Setup-Doku:** [`docker/boss-host/README.md`](../docker/boss-host/README.md)

#### Runtime: `host` — Hermes (NEU 2026-04-30)

Hermes ergänzt das `host`-Runtime-Bucket als 12. Agent (Pilot, v0.8) — siehe ADR-029.
- **Binary:** `~/.local/bin/hermes` (eigene Hermes Agent CLI, nicht claude)
- **LLM-Backend:** vLLM via `OPENAI_BASE_URL=http://192.0.2.10:8000/v1` mit `Qwen/Qwen3.6-35B-A3B-FP8` — selber Provider wie Sparky, geteilt
- **Bridge:** eigene `scripts/hermes-bridge.py` (NICHT `cli-bridge.py`, NICHT `free-code-bridge.py`) — Hermes-Binary unterscheidet sich grundlegend von claude
- **Lifecycle:** launchd `com.mc.hermes-bridge` → `hermes-bridge.py` → tmux-Session `hermes-worker`
- **DB:** `agents.agent_runtime = 'host'` + `runtimes.single_instance = true` auf der Hermes-Runtime → Switch-Service raised `AgentNotSwitchableError` (HTTP 422), generisches Pattern für künftige host-side Worker
- **NICHT in `docker/docker-compose.agents.yml`** — host-side, das File ist generator-managed via `compose_renderer.py`
- **Deliverable-Pfade:** Host-Worker wie Hermes können Deliverables mit Host-Pfaden registrieren (`~/.mc/deliverables/{task_id}/` oder `${HOME_HOST}/.mc/deliverables/{task_id}/`). Das ist dieselbe physische Datei via Volume-Mount (`~/.mc/deliverables:/deliverables` im Backend-Container). Der Backend-Validator akzeptiert beide Formen; der FileResponse-Resolver mappt Host-Form → Docker-interne Form vor der Slug-Expansion. Path-Traversal-Schutz gilt für alle Formen. Siehe ADR-031.
- **Setup-Doku:** ADR-029 + (folgende Phasen) `docs/agent-state.md`

### 6. LLM Runtime Registry (NEU 2026-04-19)

Neben den **Agent-Runtime-Typen** (cli-bridge / host — Wo läuft der Agent? — `openclaw` entfernt in v0.9) gibt es die **LLM-Runtime-Registry** (Welches Modell / welcher OpenAI-kompatible Server beantwortet Agent-Requests?).

Seit 2026-04-19 ist diese Registry DB-backed (`runtimes` Tabelle) statt JSON-hardcoded — siehe ADR-017. Seit Phase 16 (2026-04-29, ADR-028) ist die DB **alleinige Wahrheit** auch für `GET /runtimes` + `GET /runtimes/{id}` — `runtime_manager.list_db_runtimes(session)` liest direkt aus der Tabelle, `load_registry()` (JSON) wird nur noch beim Lifespan-Bootstrap verwendet. Agents haben eine optionale FK `agents.runtime_id` die bei jedem `sync-config` und im `/internal/bootstrap` gerendert wird: das Backend schreibt `OPENAI_BASE_URL` + `OPENAI_MODEL` in die agent-`.env` (Routing-Helper: `routers/internal.py::build_runtime_env(rt, session)` — claude-Image → `ANTHROPIC_AUTH_TOKEN`, openclaude-Image → `OPENAI_API_KEY` + `OPENAI_BASE_URL`); der entrypoint exportiert die gleichen Werte aus der Bootstrap-Response.

| Runtime-Type | Lifecycle | Wo | UI-Verwaltung |
|---|---|---|---|
| `lmstudio` | `lms load/unload` via SSH | Host der Runtime (Host-Registry) | Start/Stop pro Modell |
| `vllm_docker` | `docker start/stop/restart` via SSH | Host der Runtime (Host-Registry) | Start/Stop Container |
| `unsloth` | tmux `new-session` / `kill-session` via SSH | Host der Runtime (Host-Registry) | Start/Stop Studio |
| `openai_compatible` | Nur Health-Probe (remote Lifecycle) | Extern | Enable/Disable |
| `cloud` | Nur Health-Probe (z. B. Ollama Cloud) | Extern | Enable/Disable |
| `unsloth_porsche` | Start/Stop via Flask `:5555` (PowerShell) + Wake-on-LAN | Host der Runtime (Host-Registry, kind `flask_wol`) | Wecken/Start/Stop, power-managed |
| `hermes` | Host-side Hermes-Worker (launchd) | Mac | Enable/Disable |
| `omp` | Native omp-**TUI** (tmux Window 0) + `bridge.py`-Poll-Treiber (Window 1) im `mc-omp-agent` Container (ADR-049; ersetzt das Headless-`bridge.py --serve`-Modell von ADR-045) | Host der Runtime (Host-Registry, Qwen vLLM) | Enable/Disable + Switch |

#### Host-Registry (NEU 2026-07-02, ADR-048)

Seit ADR-048 sind die **Hosts** der LLM-Runtimes selbst DB-Rows: Tabelle `hosts` (Migration `0133_host_registry`, Model `backend/app/models/host.py`) mit `kind` = `ssh` | `flask_wol` | `local` plus Verbindungsdaten (`ssh_host`/`ssh_user`/`ssh_key_path`, `control_url`, `wol_mac_address`, `power_managed`). `runtimes.host_id` (FK, nullable, `ondelete=SET NULL`) bindet eine Runtime an ihren Host — damit ist das alte Muster „neue Box = neuer runtime_type + Copy-Paste-Control-Code" abgelöst (ADR-042 bleibt für die `flask_wol`-Mechanik gültig).

Auflösung via `services/host_resolver.py::resolve_host_for_runtime()` — Back-Compat-Kette: 1. `runtime.host_id` → Host-Row, 2. Legacy-Feld `runtime.host`, 3. `settings.dgx_ssh_host` (heutiges Verhalten), 4. `None` (Lifecycle-Ops liefern klaren Fehler, HTTP-only-Probes laufen weiter). `runtime_manager` arbeitet nur noch mit dem aufgelösten `ResolvedHost` (nie direkt mit `settings.dgx_ssh_*`); `get_host_metrics(host)` ersetzt `get_spark_metrics()`, Eviction ist host-scoped. Legacy-Runtime-Felder (`host`, `control_url`, `wol_mac_address`, `power_managed`) bleiben als **deprecated Fallback** erhalten. Bootstrap-Seed im Lifespan (idempotent, analog Runtime-Seed ADR-028) — **Fresh-Install ohne GPU-Host: 0 Hosts, 0 Fehler**, Cloud-Runtimes brauchen keinen Host. API: `routers/hosts.py` (CRUD admin-only, `/hosts/{id}/metrics`, Delete-Guard bei gebundenen Runtimes) + Back-Compat-Alias `GET /runtimes/spark/metrics` → Host-Slug `dgx-spark`.

#### Power-managed Runtime: PORSCHE (`unsloth_porsche`, NEU 2026-06-24, ADR-042)

PORSCHE ist eine **Windows-Box** mit lokalem **unsloth-OpenAI-Server**, die im Leerlauf **schläft**. Anders als der DGX (`unsloth`, SSH/tmux, läuft durch) hat sie einen eigenen Control-Plane und einen Power-Lebenszyklus — darum ein **eigener `runtime_type` statt DGX-Branch-Erweiterung** (null DGX-Regressionsrisiko).

- **Control-Plane:** Flask-Server auf `:5555` (`POST /powershell`, `GET /health`) statt SSH/tmux. Helper in `runtime_manager.py`: `_porsche_reachable()` (Box wach?) + `_porsche_powershell()` (analog `_ssh_run`). Start nutzt `runtime.launch_command` (PowerShell), Stop killt den Prozess am OpenAI-Port (gibt VRAM frei). Health via OpenAI `/v1/models`.
- **Neue Runtime-Felder** (Migration 0130, alle nullable/default-off → bestehende Runtimes unberührt): `control_url` (`:5555`-URL), `wol_mac_address`, `power_managed` (bool). Seed in `runtimes.json` als `enabled=false` bis echte Werte (Port/Modell/`launch_command`) gesetzt sind.
- **State-Mapping** (`get_runtime_state`, Feld `state` + Debug-Feld `container_status`): `:5555` aus → `stopped`/`asleep`; `:5555` da, `/v1` ≠ 200 → `stopped`/`booted_no_model`; `/v1/models` = 200 → `ready`/`serving`.
- **Bedarfsgesteuert:** WoL weckt nur die Box (billig); das Modell lädt erst on demand via Start (Warmup ~1–3 Min) — GPU/VRAM/Strom laufen nur bei Nutzung, kein Autostart.
- **Wake-Flow (Backend kann kein L2-Broadcast aus Docker):** `POST /api/v1/runtimes/{id}/wake` → `runtime_manager.wake_runtime()` schreibt eine Trigger-Datei nach `~/.mc/wake-requests/<slug>.request.json` (unter dem bestehenden `~/.mc`-Bind-Mount; Shape `{slug, mac, ip, broadcast, requested_at}`). Ein host-seitiger launchd-Watcher liest sie und ruft `~/.claude/skills/wake-porsche/wake_porsche.py` auf, das das Magic-Packet sendet. Endpoint ist hart auf `power_managed` gegated (400 sonst, 404 wenn Runtime fehlt).
- **Runtime-Readiness Dispatch-Gate** (`services/runtime_readiness.py`): ein Agent, dessen LLM-Hirn auf einer schlafenden PORSCHE sitzt, soll keinen Task in die Session injiziert bekommen. Konsultiert an beiden Dispatch-Einstiegen — `operations.check_dispatch_allowed` (Schritt 3.5, neuer optionaler `session`-Param; alle 6 Push-Aufrufstellen über 5 Dateien — `task_lifecycle.py` mit zweien — übergeben ihn) + `routers/agents.py::agent_poll` (nur der frische Inbox-Claim, Recovery/phase_approval unberührt). **Greift ausschliesslich für `power_managed`-gebundene Agenten:** `runtime_id` NULL, nicht-power-managed Runtime oder Kill-Switch `enable_runtime_readiness_gate=false` → sofortiger früher Return, jeder andere Agent (24/7 cli-bridge, host, DGX, cloud) läuft den unveränderten Pfad. Readiness wird ~15 s in Redis gecacht (kein `:5555`-Hämmern), und **jeder Fehler fällt OPEN** — ein Gate-Bug kann die Fleet nie stalled lassen. Ist die Box schlafend, bleibt der Task geparkt (inbox, `dispatched_at` ungesetzt) bis `ready`.
- **Default manuelles Wecken** (Auto-Wake-on-dispatch deferred als spätere Opt-in-Stufe).

**omp Runtime (NEU 2026-07-01, ADR-045):** Dritter Harness-Image-Typ neben `mc-claude-agent` (native claude) und `mc-agent-base` (openclaude). Statt einer interaktiven CLI-Pane in tmux Window 0 läuft `docker/omp-bridge/bridge.py --serve` als persistenter Poll→omp→Lifecycle-Treiber; `omp -p --mode json` ist ein kurzlebiger Subprozess, dessen strukturierter NDJSON-Stream deterministisch auf `mc ack` / `mc finish` / `mc blocked` gemappt wird (schliesst die Silent-Abort-Lücke — jeder Lauf endet terminal). omp spricht OpenAI-completions nativ und treibt Qwen auf der DGX Spark direkt (kein openclaude-Shim). Drei Routing-Branch-Points lernen `omp` **ohne Token-Routing-Duplikat**:
- **Image-Auswahl** — `compose_renderer.pick_image_for_runtime` → `mc-omp-agent:latest` (`runtime_type == "omp"`, vor der openclaude-Allowlist geprüft).
- **`.env`-Tokens** — `internal.build_runtime_env` hat einen expliziten `omp`-Branch (spiegelt `hermes`): `OPENAI_BASE_URL` + `OPENAI_MODEL`, **keine** anthropic-Tokens. Der Container-Entrypoint rendert daraus omp's native `models.yml` (`qwen-spark`-Provider, `auth: none`).
- **`docker_agent_sync`** — **kein neuer Branch**: der non-anthropic Slug `omp-qwen` nimmt den bestehenden OpenAI-Zweig (`OPENAI_BASE_URL`/`OPENAI_MODEL`/`OPENAI_API_KEY`).

Readiness re-ankert auf das `OMP_BRIDGE_READY`-Sentinel (headless omp emittiert keine Glyphe): `wait_for_agent_healthy(ready_signals=("OMP_BRIDGE_READY",))` scrapt die Window-0-Pane **auf beiden** Switch-Pfaden (auch cross-image, wo `respawn_mode=False` sonst nur `docker inspect …==running` prüfen würde → false-positive bei crash-loopender Bridge). Registrierung: idempotenter Seed-Eintrag `omp-qwen` in `backend/config/runtimes.json` (oder `docker/omp-bridge/register-omp-runtime.sh`). Alle Produktionsaktionen (Image-Build, Registrierung, Switch) sind GATED.

**omp Native-TUI Rework (NEU 2026-07-04, ADR-049 — ersetzt das Drive-Modell von ADR-045):** Damit die Sessions-Seite die **echte, scrollbare native omp-CLI** zeigt (Parität zu claude/openclaude) statt `bridge.py`-JSON-Logs, läuft jetzt **Window 0 = die native omp-TUI** (`launch-omp.sh` → `omp --hook turn-end-hook.mjs --model qwen-spark/<model> --cwd <task-cwd> --approval-mode yolo`), **Window 1 = `bridge.py --serve`** (Poll-Treiber), **Window 2 = Recycler** (trackt jetzt TUI **und** Bridge). Ablauf pro Task (alles in-container gegen echtes Qwen verifiziert):
- **Wizard-Skip:** Entrypoint setzt `omp config set startup.setupWizard false` + `setupVersion 1` (roh-geschriebenes `config.yml` wird von omp nicht geehrt) → TUI bootet direkt zum Chat-Prompt.
- **Completion via Hook, kein Scraping:** `turn-end-hook.mjs` (`api.on('turn_end', …)`) schreibt je Turn eine JSON-Zeile in ein Signal-File, das die Bridge tailt. Ein **Nicht-`toolUse`-Turn** ist terminal: `stop` → Completion-Contract (finish|silent-abort), `error`/`aborted` → Error-Familie, `toolUse`/`length` → weiter warten. Der reduzierte `RunOutcome` läuft durch das **unveränderte** `classify()`/`decide_lifecycle()`/`drive_live_run()` (gleiche Taxonomie, gleiches ack/finish/blocked + finish→blocked-Fallback).
- **Task-Injektion via `@file`:** Dispatch-Body → `$OMP_HOME/tasks/task-<id>.md`, injiziert als `@/abs/path`-Mention per `tmux send-keys` (kein Paste). Sequenz: `@path` → `Escape` (Autocomplete-Popup schliessen, Text behalten) → `Enter` (submit → omp `Read`t das File).
- **Per-Task-Isolation:** zwischen Tasks `tmux respawn-window -k` auf Window 0 mit dem neuen `--cwd` (Isolation + cwd-Rebind + frischer Kontext in einem; `/new` kann cwd nicht wechseln).
- **Silent-Abort-Watchdog (unverhandelbar):** kein terminaler Turn bis zur Per-Task-Deadline, No-Progress-Idle-Timeout, **oder** TUI-Child tot → **SIGKILL + Relaunch** der TUI + Task → `blocked` (`ABORT_HANG`), nie `in_progress`.
- **Readiness-Anker verschiebt sich** von `OMP_BRIDGE_READY` (jetzt in Window 1) auf die **TUI-Chat-Glyphe** (`╭─`/`❯`/`> `) in Window 0 — impliziert eine **einzeilige Backend-Folgeänderung** (für `runtime_type=="omp"` die Default-Glyphen statt des Sentinels an `wait_for_agent_healthy` übergeben; `agent_runtime_switch.py`).

**Switch-Semantik (Phase 15, 2026-04-28):** Runtime-Wechsel eines cli-bridge Agents läuft atomar durch `services/agent_runtime_switch.switch_agent_runtime` (ADR-027 erweitert ADR-018):

1. Validate (runtime exists, enabled, agent ist cli-bridge, soft-warnings für tools/state-mismatch).
2. In-progress Block (`current_task_id`) — Force-Toggle in der UI.
3. Redis-Lock `mc:agent:{id}:runtime-switch` (TTL 120s) gegen Concurrency.
4. Bei Image-Wechsel (claude ↔ openclaude): `compose_renderer.write_compose_agents()` rendert `docker/docker-compose.agents.yml` aus dem DB-State BEVOR der Container angefasst wird. Atomic write + `.bak`-Backup.
5. DB-Commit `agent.runtime_id` → `sync_docker_agent_files()` (`.env` + `settings.json`) → `restart_docker_agent_container(...)`. Drei Modi seit Phase 16 (ADR-028): **`respawn_window_only=True`** für Same-Image-Switches — nur `tmux respawn-window -k -t {slug}:0`, poll.sh + Recycler überleben (<5s). **`force_recreate=True`** für Cross-Image-Switches (claude ↔ openclaude) — `docker compose up -d --force-recreate <service>` (~30–90s). **Default** — `docker restart -t 5` für Env-Refresh ohne Image-Wechsel.
6. `wait_for_agent_healthy` mit Mode-Awareness: `respawn_mode=True` → pollt `tmux capture-pane` auf Ready-Signale (`╭─` / `❯` / `> ` / `$ `) und dismissed Modell-Picker einmalig per Enter; sonst → `docker inspect` auf Container-State. On fail: full rollback (DB + Files + Compose + Container) + `agent.runtime_switch_failed` Event.
7. On success: Redis-Publish auf `mc:agent:{id}:terminal:remount` (Sessions-Seite re-mountet WebSocket automatisch) + `agent.runtime_switched` Activity-Event.

UI: Dropdown im Config-Tab + RuntimeSwitchModal (dry-run preview, Image-Banner, Compat-Warnings, Force-Toggle). Bound-Agents Footer auf RuntimeCards (`/runtimes`) zeigt aktive Bindings + BindAgentModal für direktes Binden ohne AgentDetailPage. AgentCard Mini-Grid zeigt RuntimePill (compact). Validation: nur `agent_runtime = cli-bridge` kann `runtime_id` setzen; Boss + Hermes (host, single-instance) zeigen einen Locked-Badge.

**Migration Pfad:** JSON (`backend/config/runtimes.json`) ist nur noch Seed-Source. Beim Startup seeded der Lifespan-Hook `_seed_runtimes` alle fehlenden Einträge nach Slug in die DB (idempotent). UI/API CRUD-Endpoints: `POST/PATCH/DELETE /api/v1/runtimes/db`.

#### Harness/Provider-Decoupling (NEU 2026-07-05, ADR-056)

Vor ADR-056 steckte der **Harness** (welches CLI-Binary treibt den Container — Claude Code / OpenClaude / omp) implizit in der gebundenen Runtime (`runtime_type`/Slug-Präfix), gestreut über drei Dateien. Seit ADR-056 sind das **zwei unabhängige Achsen**: `agents.harness` (Migration `0143`, nullable — `claude`|`openclaude`|`omp`) und die Runtime als reiner Provider. `backend/app/services/harness_compat.py` ist die zentrale Matrix: `runtime_protocol(runtime)` klassifiziert `"anthropic"`|`"openai"`|`None`, `HARNESS_PROTOCOLS` definiert die v1-Kompatibilität (`claude`→anthropic, `openclaude`/`omp`→openai), `derive_harness(runtime)` ist der Legacy-Fallback für Agents mit `harness IS NULL` (Backfill via Migration `0143`). **Image folgt Harness, nicht Runtime:** `compose_renderer.pick_image_for_harness(harness, runtime)` schlägt zuerst `HARNESS_IMAGES[harness]` nach, fällt nur bei `harness=None` auf die alte `pick_image_for_runtime`-Kopplung zurück. `agent_runtime_switch.switch_agent_runtime` hat eine zweite Switch-Achse (`new_harness`); inkompatible Kombinationen (z. B. `claude` gegen einen OpenAI-Protokoll-Runtime) werden vor jeder Mutation mit `incompat_reason` abgelehnt. **Provider-Key-Auflösung** (`resolve_provider_credentials`, gemeinsame Quelle für `/internal/bootstrap` und `.env`-Render): `agent.secret_id` > `runtimes.api_key_secret_id` (neue Spalte, schreibbar über `routers/runtimes.py`). Kein globaler Fallback mehr (Amendment 2026-07-05, ADR-056 Finding 5) — ein früherer globaler `ollama_api_key`-Fallback liess jeden OpenAI-Protokoll-Runtime, auch lokale schlüssellose vLLM/LM-Studio-Instanzen, unbemerkt den bezahlten Cloud-Key erben; resolviert keine der beiden Stufen, wird schlicht kein `OPENAI_API_KEY` gesetzt. `GET /runtimes/compat-matrix` liefert `compatible_harnesses`/`incompat_reason` pro Runtime für den Frontend-Harness-Selector (RuntimeSwitchModal, Add-Runtime-Wizard). **Nicht im Scope (v2):** `claude`×OpenAI und `omp`/`openclaude`×Anthropic brauchen einen LiteLLM-Proxy-Shim zur Protokoll-Übersetzung — bewusst geparkt, kein Hot-Swap ohne Restart.

#### Runtime Watcher — model-drift auto-detection (NEU 2026-07-05, ADR-054)

"Engine leads, MC follows": `services/runtime_watcher.py` is a singleton
background loop (same pattern as `intelligence.py` — asyncio task, Redis
lock for multi-worker dedup, `settings.runtime_watcher_enabled` kill-switch,
`settings.runtime_watcher_interval` default 90s) that supersedes **D-22**
(ADR-028's "no periodic background probing" call — see ADR-054 for the
full reversal rationale). Every tick it probes all `enabled` runtimes of a
probeable `runtime_type` (`vllm_docker`, `lmstudio`, `openai_compatible`,
`unsloth`) via `GET {endpoint}/v1/models` (reuses the Phase-15
`probe_runtime_model` helper) and writes a live snapshot to Redis
(`mc:runtime-live:{slug}`, TTL 3×interval — `reachable`, `served_model`,
`latency_ms`, `last_probe_at`), feeding the `/runtimes` cockpit live-dot
via `GET /api/v1/runtimes/live-status`.

Drift (`served_model != runtime.model_identifier`) is only acted on after
**two consecutive identical probes** (guards against flapping during
engine warm-up); a confirmed drift persists `model_identifier`, invalidates
the resolver cache, emits `runtime.model_changed`, and flags every bound
cli-bridge agent `pending_runtime_sync = true`
(`services/runtime_propagation.py::mark_agents_for_sync`, column added by
Migration `0141`). Unreachable endpoints only update live status;
`runtime.unreachable` fires after 3 consecutive failed probes.

**Propagation** (`services/runtime_propagation.py`) runs a sync pass at the
end of every tick: idle flagged agents get `sync_docker_agent_files()` +
a **plain `docker restart`** (re-runs the container entrypoint →
`/internal/bootstrap` → fresh `OPENAI_MODEL`/`OPENAI_BASE_URL`) —
deliberately **not** `respawn_window_only` (ADR-028's same-image fast
path), because a window respawn keeps the stale tmux environment and would
never pick up the new model. Busy agents (`current_task_id` set) stay
flagged and are retried by the *next* watcher tick (≤90s after the task
ends) rather than via a `task_lifecycle` hook — no new coupling into the
task-completion path. A Redis failure counter trips a circuit breaker after
3 failed sync attempts (`agent.model_sync_failed`, agent left as-is — no
restart-loop). Force path for operators who don't want to wait:
`POST /runtimes/db/{slug}/sync-agents`.

**omp** provider was renamed `qwen-spark` → `mc-openai` and its hardcoded
Spark model defaults removed from the seeds (`runtimes.json`,
`register-omp-runtime.sh` now ship `model_identifier: null`) — the first
probe fills it in, closing the last "MC leads" hole in the omp boot path.

**Runtime-Switch-Progress (NEU, ADR-054):** `RuntimeSwitchModal` polls
`GET /api/v1/agents/{id}/runtime-switch-progress` (published by
`agent_runtime_switch.publish_switch_progress`) for an explicit stepper —
`rendering → restarting → waiting_healthy → done | rolled_back` — instead
of a fire-and-forget confirm dialog.

#### CLI-Tool-Updates — Manifest + Host-Bridge-Build + Rolling Recreate (NEU 2026-07-05, ADR-058)

Die drei Agent-CLI-Tools (`openclaude`, `claude`, `omp`) waren bisher
uneinheitlich in den Dockerfiles gepinnt (`openclaude` als Literal, `claude`
ungepinnt/`latest`, nur `omp` bereits versioniert per `ARG`+sha256). Ein
Update verlangte Dockerfile-Edit + manuellen Rebuild + manuelles Recreate.
Seit ADR-058 ist `docker/cli-versions.json` die **Single Source of Truth**
für Soll-Versionen (+ `omp`-sha256): `scripts/build-agent-images.sh` liest
sie für die Build-Args, jedes Image trägt zusätzlich die OCI-Labels
`mc.cli.name`/`mc.cli.version`/`mc.image.built-at` für den Ist-Stand
(`docker image inspect`, kein separates Tracking).

`services/cli_update_check.py` läuft als Singleton-Loop (Muster wie
`runtime_watcher.py`, `settings.cli_update_check_interval` Default 6h,
`0` = aus) und cached `{installed, target, latest, update_available}` je Tool
in Redis (`mc:cli:versions`) — Quellen sind die npm-Registry
(`openclaude`/`claude`) und GitHub Releases (`omp`, `can1357/oh-my-pi`).
`target=None` zählt bewusst nicht als Update-Signal, und ein Fund löst nur
das dedupte Event `cli.update_available` aus — kein Auto-Update.

**Build läuft auf dem Host, nie im Backend-Container** — der
Docker-Socket-Proxy hat `BUILD: 0` (ADR-047). `scripts/cli-bridge.py`
(dieselbe Bridge, über die auch Plugin-Installs laufen, Port 18792) bekommt
dafür `POST /agent-images/build` (Hintergrund-Subprozess, Log-Datei, 409 bei
laufendem Build), `GET /agent-images/build/status` (Polling) und
`POST /agent-images/omp-sha256` (TOFU-Digest, falls die GitHub-Release keinen
Asset-Digest liefert).

`services/cli_update_runner.py` orchestriert den Klick-Ablauf hinter einem
Redis-Lock (`mc:cli:update-lock`, TTL 1800s): Manifest bumpen → Bridge-Build
triggern + Fortschritt nach `mc:cli:update-progress` pollen → bei
Build-Fehlschlag Manifest-Rollback (alter Image-Tag bleibt unberührt, Event
`cli.update_failed`) → bei Erfolg Rolling Recreate der betroffenen
Harness-Agents (`agents.harness`, ADR-056): idle sofort `force_recreate`,
busy → `agents.pending_recreate = true` (Migration `0146`,
`services/runtime_propagation.mark_agents_for_recreate`/
`recreate_pending_agents`, läuft im selben Watcher-Tick nach
`sync_pending_agents` — dieselbe ADR-054-Propagationsmechanik, aber
`force_recreate` statt `docker restart`, weil sich das Image geändert hat).
Circuit-Breaker nach 3 Fehlversuchen wie ADR-054. Erfolg emittiert
`cli.updated`; die Manifest-Änderung bleibt bewusst uncommitted im Repo —
Commit ist Sache des Users.

API `routers/cli_tools.py` unter `/api/v1/cli-tools`: `GET ""` (Liste,
`require_user`), `POST /check`, `GET /update-status` (Polling), `POST
/{tool}/update` (202, nur `operator`-Rolle, 409 bei laufendem Update).
Frontend: neue Sektion "CLI-Tools" auf `/runtimes`
(`CliToolsSection.tsx`) — Ist/Latest/Update-Badge je Tool, Bestätigungsdialog,
Fortschrittsanzeige (Phasen: Manifest → Build (Log-Tail) → Recreate).
**Nicht in v1:** GHCR-Publish der Agent-Images, Auto-Update-Policy,
Changelog-Anzeige im Dialog.

#### Sparkrun Recipe-Switching — Solo-Capability-Guard (NEU 2026-07-06, ADR-059)

Der DGX Spark hat **1 GPU**. `sparkrun` (die Recipe-CLI, die vLLM-Container auf
dem Spark steuert) bietet für dasselbe Modell teils mehrere Registry-Varianten
an — eine `@official`-Variante mit `tp=1`/`nodes=1` (solo-fähig) und
`@eugr`/`@community`-Varianten mit `tp=2`/`tp=4` + `vllm-ray`-Backend für
Multi-GPU-Cluster. `services/sparkrun_manager.list_recipes()` parste bisher
nur `name`/`model`/`registry` aus `sparkrun list` und **verwarf die TP/Nodes-
Spalten** — genau das Signal, das solo- von cluster-Recipes unterscheidet.
`build_launch_command()` setzte nie `--tensor-parallel` (`--solo` steuert nur
den Ray-Node-Bootstrap, nicht den tp-Wert) — ein Recipe-Switch auf eine
tp=2-Variante schlug auf dem 1-GPU-Host still fehl ("engine unreachable").

**Fix:** `list_recipes()` parst `tp`/`nodes` (Spalten 2/3, `-` → `None`) und
berechnet `solo_capable` gegen die **tatsächliche** GPU-Zahl des Ziel-Hosts
(`get_host_gpu_count()`, `nvidia-smi -L | wc -l`, host-scoped über den ADR-048
`ResolvedHost`-Chain, Fallback `1` bei SSH-Fehler — nie hartkodiert).
`switch_recipe()` konsultiert das VOR jedem Evict:

- **`nodes > 1`** → Switch abgebrochen (Activity-Event
  `runtime.recipe_switch_rejected`) **bevor** der aktuell laufende Container
  evicted wird — ein Multi-Node-Recipe kann dieser Single-Host-Deployment nie
  gelingen, ein unwinnbarer Switch darf nicht erst das gesunde Modell killen.
- **`tp > host_gpu_count`, `nodes <= 1`** → `build_launch_command(...,
  tp_override=host_gpu_count)` injiziert `--tensor-parallel <N>` und der
  Switch läuft weiter (best-effort — ob das Modell bei weniger VRAM/GPU passt,
  entscheidet nur vLLM selbst, siehe unten).
- Recipe unbekannt (nicht in `sparkrun list`) oder der Guard selbst nicht
  erreichbar (SSH-Fehler) → Switch läuft ohne Guard weiter statt auf
  fehlender Information zu blockieren.

**Zweiter Fix (derselbe Vorfall):** `runtime_manager.start_runtime()` prüfte
bisher nur, ob ein Container mit dem `mc.runtime.slug`-Label erscheint
(`verify_spark_container_started`) — nicht, ob vLLM darin wirklich läuft.
Manche Launches (sparkrun-Solo-Wrapper, manuelle Container) halten PID1 als
`sleep infinity`, während vLLM als separater, out-of-band gestarteter Prozess
läuft — der kann sterben (falsches tp, OOM, Crash), während der Container
"running" bleibt. Neuer zweiter Check `verify_spark_vllm_process_started`
pollt `docker top` auf einen echten `vllm serve`-Prozess (gleicher Scan wie
`_container_runs_vllm_server` für Discovery) bevor `start_runtime` Erfolg
meldet — schliesst genau die Silent-Failure-Lücke aus dem Vorfall (sparkrun
meldet `exit 0` fire-and-forget, nichts serviert tatsächlich).

Frontend: `SparkRecipeSwitcher` zeigt `tp`/`nodes` als Badge und deaktiviert
nicht-solo-fähige Recipes (Tooltip + Hinweistext), statt sie gleichwertig
anklickbar zu lassen.

Als Nebenbefund verifiziert (kein Code-Fix nötig): der Agent-Restart-
Propagationspfad (`services/runtime_propagation.py` → `docker_agent_sync.
restart_docker_agent_container`) kann strukturell nie einen sparkrun-Modell-
Container treffen — der Container-Name kommt ausschliesslich vom Agent-Slug
(`mc-agent-<slug>`), nie von einem Runtime-/Modell-Identifier. Eine
Assertion + Regressionstest (`test_agent_restart_never_targets_runtime_
container.py`) machen das als Tripwire fest. Der `docker restart`, der beim
Vorfall den sparkrun-Container zurücksetzte, kam stattdessen vom manuellen
Restart-Button (`runtime_manager.restart_runtime()`, `/runtimes/{id}/restart`)
— erwartetes Verhalten für einen bewussten Restart-Klick, aber ein Hinweis,
dass ein Restart auf einem sparkrun-Solo-Container den injizierten vLLM-
Prozess killt, ohne ihn neu zu starten (v2-Kandidat, nicht in diesem Fix).

---

## Zentrale Flows

### Task-Lifecycle

```
inbox → in_progress → review → done
  ↓         ↓            ↓       ↓
blocked  failed      (re-open: → in_progress)
  ↓
failed (re-open: → inbox)
```

**Auto-gesetzte Felder:**
- `started_at` bei → `in_progress`
- `completed_at` bei → `done`
- `dispatched_at` wenn `chat_send()` erfolgreich ODER beim ersten `/agent/me/poll` (Task bleibt `inbox`)
- `ack_at` wenn Agent PATCH `status: in_progress` (= ACK-Handshake)

**Poll-vs-PATCH-Split (Plan 26-02, HERM-10/F1+F3):** Der Endpoint `GET /agent/me/poll`
liefert nur den Prompt aus + setzt `dispatched_at` (Status bleibt `inbox`, `ack_at` bleibt
NULL). Status flippt erst durch den expliziten Agent-PATCH `status:in_progress` — dort
werden `started_at` UND `ack_at` gemeinsam gesetzt. Damit ist `dispatched_at < ack_at`
mit messbarer Spanne garantiert (kein gemeinsames `now`-Literal mehr).

**Auf MC Dev Board**: Tasks müssen durch `review` bevor sie `done` werden können. `failed` kann nur zu `inbox` re-opened werden.

### Dispatch-Flow (Docker V2 aktiv)

1. **Task Creation** — `POST /boards/{id}/tasks` oder Planner-Phase-Instanziierung
2. **Auto-Dispatch** — `dispatch.auto_dispatch_task()`:
   - `find_dispatch_target()` wählt nach Priorität: explizit assigned → Orchestrator → Board Lead → erster Online-Agent
   - `_load_dispatch_context()` lädt parallel (asyncio.gather): Board Memory, Agent Lessons, Intelligence, Projekt-Kontext, Git-Info
   - `_build_dispatch_message()` erzeugt Structured Message mit Task-Details + Curl-Callbacks + Projekt-Kontext + ACK-Instruktion
   - `rpc.chat_send()` (Board Lead, Haupt-Session) oder `rpc.chat_send_isolated()` (Worker, eigene Task-Session)
   - `dispatched_at = now()`, Task bleibt **inbox**
3. **Agent ACK** — Agent sendet `PATCH /agent/boards/{board_id}/tasks/{task_id}` mit `{"status": "in_progress"}`
   - Backend validiert Scopes + Board Rules + `dispatch_attempt_id` Header
   - Setzt `started_at`, `ack_at`
   - Emit `task.ack_received`
4. **Work** — Agent postet Progress-Kommentare (`comment_type: progress/evidence/next`)
   - Task Runner (60s): ACK-Timeout > 10min → Approval an den Operator (nicht Auto-Reassign!)
   - Stale Progress > 60min → Discord-Warning, nach 3x → Circuit Breaker
   - **Silent-Abort-Auto-Block (ADR-046, cli-bridge v1):** acked Task, aber Agent verstummt (`last_task_activity_at` stale > `stuck_block_minutes`, default 25min/45min-slow) bei lebendem Wrapper (`last_seen_at` frisch) → tick 1 nudge, tick 2+ → `blocked` via `apply_terminal_unassign` + `blocker_decision`-Approval (Telegram). Nur cli-bridge (host/manual hard-skip, Prime Directive). `task_runner._check_stuck_in_progress`
   - Auto-Promote: letzter Kommentar `comment_type: resolution` → Task automatisch zu `review`
5. **Review** — Subtask → `done`, Root-Task → `review`
   - Bei Git-Projekt: `GitService.create_pr()` (squash-merge)
   - Reviewer (Rex) bekommt isolierte Review-Session
   - Bei Ablehnung: `review` → `in_progress` (Kontext bleibt), bei Genehmigung: `review` → `done` + PR merge
6. **Phase-Completion** (Watchdog 30s): Alle Subtasks `done` → Parent auto `review`
7. **Callback** — `callback_agent_id` bekommt Done-Notification (Fallback: Board Lead)

### Agent-Docker-Task-Loop

```
poll.sh (Window 1)
  ↓ HTTP GET /api/v1/agent/me/next-task (alle 5s)
  ↓ Task-JSON mit prompt
  ↓ tmux load-buffer /tmp/prompt.txt
  ↓ tmux paste-buffer -t {agent}:0
  ↓ tmux send-keys Enter
    ↓ openclaude in Window 0 verarbeitet
    ↓ Output via PTY → docker exec → WebSocket → Browser
  ← poll.sh prüft Output-Stability (6×5s unchanged = fertig)
  ← Heartbeat POST /api/v1/agent/me/heartbeat (alle 30s)
```

### Knowledge Base Scoping

Ein `board_memory` Table speichert drei Scopes über `board_id`/`agent_id` null-checks:
- **board_id set, agent_id null** → Board Memory (alle Board-Agents sehen)
- **board_id set, agent_id set** → Agent-private Knowledge
- **beide null** → Global Knowledge (team-weit)

Memory-Typen: `knowledge | decision | lesson | reference | journal | concept | weekly_review | insight | research`.

### 3-Layer Memory System (Qdrant, seit 2026-04-11)

Jeder Memory-Eintrag wird automatisch in eine von 3 Qdrant-Collections indexiert:

| Layer | Collection | Memory-Types | Zweck |
|---|---|---|---|
| **Semantic** | `memory_semantic` | knowledge, decision, concept, reference, research | Wiederverwendbares Wissen, team-weit |
| **Agent** | `memory_agent` | lesson (mit agent_id) | Agent-private Lessons, lernbar |
| **Episodic** | `memory_episodic` | journal, weekly_review, insight | Zeitgebundene Events, Recency-Boost |

**Embedding:** nomic-embed-text-v1.5 (768-dim) via Spark/LM Studio auf 192.0.2.10:1234.

**Dispatch-Context:** Jeder Task-Dispatch bringt Top-3 Semantic + Top-3 Agent-Treffer via Qdrant-Query mit (Score-Threshold 0.3). Verifiziert mit Score 0.85.

**Reflection-Pipeline (Closed Loop):**
1. Agent schreibt `comment_type: reflection` mit 4 Pflichtfeldern (Was gemacht / Was funktioniert / Was unklar / Lesson)
2. `enforce_reflection=True` blockiert Status-Uebergang ohne Reflection (min. 80 Zeichen)
3. Lesson aus Reflection wird als `BoardMemory(memory_type=lesson, agent_id=...)` extrahiert
4. Auto-Index in Qdrant agent-layer → naechster Dispatch bringt die Lesson mit

**Recency-Boost (episodic):** 30-Tage linear Decay, max 25% Score-Boost. Events < 1 Tag bekommen vollen Boost.

**Fail-Soft:** Wenn Spark/Qdrant down → Memory wird trotzdem in DB gespeichert, Backfill-Script nachtraeglich ausfuehrbar. Frontend zeigt "keyword fallback" Badge.

**Frontend `/memory`:** 3-Layer-Tabs (Episodic Timeline / Semantic Card-Grid / Agent Lesson Matrix) + Scope-Dropdown (Global/Board/Agent) + MemoryQueryBar (Vektor-Suche ueber alle 3 Layer).

### Boss-Autonomy (seit 2026-04-11)

Boss (Henry) ist der zentrale Orchestrator mit neuem SOUL:
- **Memory-First:** Vor jeder Entscheidung semantische Memory-Query, dann handeln
- **Callback-Wait:** Bei delegierten Tasks `waiting_on_callback` Kommentar posten, nicht weiterarbeiten bis Watchdog reaktiviert
- **Spawn-Approval:** Neue Docker-Agents nur mit Genehmigung des Operators (via Question-Funktion)
- **Reflection-Pflicht:** Vor Task-Abschluss 4-Feld-Reflection, min. 80 Zeichen
- **Plugin-Self-Service:** Boss kann `PATCH /agents/{id}/plugins` fuer sich und Worker setzen

**Planner entfernt** (Phase 6 + D): `planner.py` Router, `planner_mode` DB-Feld (Migration 0071), Delegation-Guards, Dispatch-Gating — alles rausgerissen. Henry uebernimmt Planung direkt.

### Multi-Agent Konsens-Helper (seit 2026-04-12)

`POST /api/v1/agent/consensus` — Boss dispatcht dieselbe Frage an N Agents (2-6), wartet parallel:
1. Root-Task als Container (`task_type: consensus`, `status: in_progress`)
2. N Subtasks (`task_type: consensus_subtask`, je einer pro Agent, auto-dispatch)
3. Watchdog erkennt Phase-Completion → Parent → review
4. `GET /api/v1/agent/consensus/{id}` liefert Status (pending/partial/complete) + alle Antworten

### Git Workflow für Coder-Agents

1. Planner finalisiert Plan → `GitService.create_repo()` (private GitHub) → `github_repo_url` in `Project`
2. Dispatch: `GitService.ensure_workspace()` (Clone) + `create_task_branch()` (`task/{slug}`)
3. Agent status `review` → `GitService.create_pr()` (squash-merge PR auf main)
4. Reviewer status `done` → `GitService.merge_pr()` (squash-merge + branch delete)

Ad-hoc Tasks ohne Projekt nutzen ein `<GITHUB_OWNER>/mc-workspace` Shared-Repo.

### Install-Flow (Phase 1)

```
Agent/Boss (curl POST /api/v1/agent/install-requests)
  → Allowlist-Check + Scope-Check + Already-Installed-Check + Duplicate-Check
  → Approval(action_type=install_skill|..., status=pending, expires_at=now+7d)
  → SSE: approvals_events fires
  → Operator sees InstallRequestCard in Inbox
  → Operator clicks Approve/Reject → POST /api/v1/approvals/{id}
  → approvals.py resolve_approval() hook triggers InstallExecutor.execute()
  → Executor: update Agent.cli_skills/cli_plugins → service-layer install
    → on success: write install_log(result=success) + sync-config (Redis lock)
    → on failure: rollback cli_skills/cli_plugins → write install_log(result=rolled_back)
  → Agent picks up change on next sync or restart
```

Siehe ADR-015.

### MCP-Sync-Flow (Phase 2)

```
Admin setzt Agent.mcp_servers (Frontend Matrix oder PATCH /agents/{id}/mcp-servers)
  → sync_agent_mcp_to_disk(agent)
  → liest ~/.openclaw/mcp-servers/*/manifest.json
  → filtert via Allowlist (null=alle, []=keine, [...]=Liste)
  → schreibt ~/.openclaw/agents/<slug>/claude-config/.mcp.json
  → stdio-MCPs verfügbar unter /mc-servers/<name>/... im Container via Shared-Mount
  → Claude Code im Container liest .mcp.json beim nächsten Session-Start
```

Siehe ADR-016.

### Intelligence-System

Singleton-Loop alle 5min (konfigurierbar). Rule-based Analyse parallel via `asyncio.gather()`:
- Task Durations (7d), Agent Performance, Failure Patterns (Keyword-Matching), Anomalies
- Stündliche AgentMetrics-Snapshots
- Optional täglich: LLM-Destillation via Ollama `qwen2.5-coder:14b` → BoardMemory (`memory_type: insight`, `auto_generated=True`)
- Redis-Cache `mc:intelligence:insights` (10min TTL)

Frontend `/insights`: 7 Sektionen (KPIs, Agent-Performance BarChart, Task-Duration-Balken, Error-Pattern PieChart, Anomalies Cards, Daily LLM Reports).

### Runtime-Drift & Propagation (NEU 2026-07-05, ADR-054)

```
vLLM / LM Studio: model changed at the engine
  → Watcher probe (≤90s): served_model ≠ runtime.model_identifier
  → two-probe confirmation (guards against warm-up flapping)
  → Runtime row updated + resolver cache invalidated + runtime.model_changed event
  → agents bound to this runtime (agent.runtime_id == runtime.id):
      idle  → sync_docker_agent_files() + docker restart (re-bootstrap) → agent.model_synced
      busy  → pending_runtime_sync=true → banner → synced on next watcher tick (≤90s after task ends)
      3 failed sync attempts → circuit breaker → agent.model_sync_failed (manual restart required)
  → /runtimes cockpit shows live model + drift-/pending-sync badges until synced
```

Down-detection is separate: an unreachable endpoint only updates the live
status (`mc:runtime-live:{slug}`); `runtime.unreachable` fires after 3
consecutive failed probes, and the `runtimes` row is left untouched (no
false "drift to nothing"). Host agents (Boss/Hermes/Jarvis) are excluded
from auto-sync — launchd-managed, they only get the activity event. Siehe
ADR-054 (supersedes D-22, ADR-028).

### Vault (Karpathy-Wiki Memory) — live (M.1-M.5 + Boss/Jarvis on main 2026-05-15)

- **Pfad:** `~/.mc/vault/`
- **Source of Truth** für Lessons, Decisions, Knowledge, Concepts. BoardMemory deprecated.
- **Services:** `vault_watcher`, `vault_compactor`, `vault_index`, `vault_embeddings`, `vault_activity`, `vault_git`, `vault_similarity_edges`, `vault_wikilink_backfill`, `vault_cleanup`
- **Schreiben:** Agents direkt FS (`agents/{slug}/`) ODER via Inbox (`_inbox/`, cross-agent)
- **Lesen:** Backend, Frontend `/memory` (Liste + Graph Tabs), Jarvis (xAI Grok via voice-worker), Obsidian-App
- **Index:** SQLite FTS5 (`.mc_index.db`), auto-rebuild bei Schema-Migration (Commit `81101319`)
- **Embeddings:** Spark DGX (Qwen3.6 + nomic-embed) → Qdrant `memory_vault`
- **Spec:** `docs/superpowers/specs/2026-05-14-mc-memory-vault-as-source-design.md`
- **ADR:** [ADR-034](decisions/034-vault-as-source-of-truth.md) — **Status Accepted**
- **Migration auf main:** `0b35ed83` (Merge feature/vault-memory-foundation, 102 commits)

**Agent Coverage:**
- 8 docker cli-bridge Agents (Sparky, Davinci, Rex, FreeCode, Researcher, Tester, Deployer, Shakespeare) — `/vault:rw` Mount + Vault-Section in TOOLS.md
- Boss (host-runtime, native claude CLI) — host-path agent.env + regenerated TOOLS.md mit `runtime="host"` Phrasierung
- Jarvis (host-runtime, xAI Grok worker im voice-worker Container; früher "Voice", siehe ADR-038) — `voice_worker/mc_client.py:vault_*` Function-Tools, kein TOOLS.md nötig
- Henry (openclaw gateway) — bewusst draussen

**Frontend `/memory` (M.4 — Obsidian-style 2D Graph):**
- `VaultMemoryPage.tsx` mit Liste + Graph Tabs
- `MemoryGraph2D.tsx` — react-force-graph-2d (Canvas), library defaults + d3-force overrides
  - `nodeRadiusFromLinkCount` 2-18px sqrt scaling (hub hierarchy)
  - charge=-300, linkDistance=25, forceX/Y(0).strength(0.12) für sphärische Form
  - filter-aware edge dimming, kein Auto-zoomToFit (Operator-feedback iteration)
  - 1-2s settle animation, dann statisch
- Hooks: `useVaultGraph`, `useVaultStream`, `useVoiceHighlight`, `useVaultSearch`, `useVaultList`, `useVaultNote`

**Backend Routes:**
- `GET /vault/notes`, `/search`, `/note/{path}` (read, user-JWT)
- `POST /agent/vault/note` (write, agent-token, related_notes optional)
- `GET /vault/graph` (k-means Clustering + Wikilinks + Qdrant similarity edges + Heatmap)
- `GET /agent/vault/briefing` (Jarvis pre-session context)
- `WS /vault/stream` (Live-Updates), `WS /vault/voice-highlight` (Jarvis → Redis Pub/Sub)
- `POST /voice/graph-highlight` (Jarvis → Redis publish für Frontend-Filter-Commands)
- Admin: `POST /vault/_admin/rebuild` (FTS5 re-index aus Filesystem)

**Migrations:**
- Alembic 0112 — `board_memory → vault` Cutover (881 rows, id-Backfill, 884 Phase-7-Legacy archiviert)
- Alembic 0114 — Boss + Voice vault-scope grants (idempotent; historischer Name, Voice → Jarvis via ADR-038/0120)
- Alembic 0120 — Voice → Jarvis Rename (agents.name + activity_events.title)

**Lessons gespeichert in:**
- `~/.claude/projects/<project-slug>/memory/feedback_d3_force_center_strength_noop.md` (operator-lokales Claude-Memory)

---

## Kritische Design-Entscheidungen (Übersicht)

Alle ADRs in `docs/decisions/`:

- **ADR-001** — Dispatch ACK Handshake (Task bleibt inbox bis Agent explizit bestätigt)
- **ADR-002** — Subagent Dispatch mit Kill-Switch (`chat_send_isolated` für Worker, `chat_send` für Board Lead)
- **ADR-003** — Triple-Runtime-Architektur (openclaw + cli-bridge Host + Docker-V2 parallel) — *openclaw-Branch entfernt in v0.9 (ADR-039)*
- **ADR-004** — BoardMemory unified (single Table mit Triple-Scoping statt 3 separate Tabellen)
- **ADR-005** — Board-Lead-First Dispatch (Henry orchestriert alle Tasks, explizite Delegation)
- **ADR-006** — Jinja2-Template als Single Source of Truth (Agent-Config in Git, nicht DB)
- **ADR-007** — Structured Dispatch Messages (Curl-Callbacks self-contained statt TOOLS.md-Lookup)
- **ADR-008** — Phase-Completion via Watchdog (zentral orchestriert, nicht Agent-getriggert)
- **ADR-009** — Agent-Scoped Router separat (`agent_scoped.py` vs `agents.py`, Scopes statt User-Rollen)
- **ADR-010** — Redis-Cache für PBKDF2 (`SHA256(token) → agent_id`, 5min TTL)
- **ADR-011** — HTTP-Polling für Docker-Agents (statt Push/Webhooks)
- **ADR-012** — `use_alter=True` ForeignKeys für Agent↔Task / Board↔Project Zyklus-Break
- **ADR-013** — Settings.json als echte Kopie (kein Symlink) im Docker-Mount
- **ADR-014** — Boss runs as macOS host process (claude binary, OAuth-Login, launchd + ttyd)
- **ADR-015** — Install-Approval Flow for Boss (agent-scoped install-requests, InstallExecutor, Allowlist, 7d TTL)
- **ADR-016** — MCP-Registry + Sync (Filesystem-basierte Registry unter `~/.openclaw/mcp-servers/`, per-Agent Allowlist)
- **ADR-017** — Runtime Registry in DB (JSON als Seed, per-Agent Runtime-Switching)
- **ADR-018** — Runtime-Wechsel via Container-Restart (kein Hot-Reload, `docker restart`)
- **ADR-019** — Claude Fleet Hybrid (9 Docker-Agents auf claude-code, Sparky + Boss unverändert)
- **ADR-020** — Harness Phase 2 (`mc` CLI + Dispatch Split + TaskChecklistItem als Progress SSoT)
- **ADR-021** — Agent Personas (Grounded Identities + Shared Reflection Charter in `app/constants.py`)
- **ADR-022** — `~/.mc/` Home + Standardized Workspace Layout
- **ADR-023** — Review-Policy: Trust-by-Default + Reflection-Decoupling
- **ADR-024** — Claude-Process Recycling im Docker-Agent-Container
- **ADR-025** — Dispatch & Agent-Scoped Split (Phase 4)
- **ADR-026** — Context Management & Auto-Recovery (Draft)
- **ADR-027** — Universal Agent ↔ Runtime Binding (atomic switch + image-aware lifecycle, supersedes ADR-018-Pfad)
- **ADR-033** — Secrets vs Credentials: Boundary kodifizieren statt unifizieren
- **ADR-034** — Vault as Source of Truth (Karpathy-Wiki Memory, M.1 Read Foundation)
- **ADR-039** — OpenClaw Gateway Sunset (Runtime + Code-Pfad + DB-Schema + Frontend + Host-Service entfernt, v0.9)
- **ADR-042** — unsloth_porsche power-managed Runtime (PORSCHE Windows-Box, Flask `:5555`/PowerShell statt DGX-SSH, Wake-on-LAN via Host-Helper, bedarfsgesteuerter Lebenszyklus, fail-open Runtime-Readiness-Dispatch-Gate nur für power-managed Agenten)
- **ADR-046** — Lifecycle Safety Watchdog (Silent-Abort Auto-Block; acked+verstummt → `blocked`; cli-bridge v1, host deferred; Prime-Directive-safe: runtime-gate + konservativer geflooter Threshold + Korroboration + staged nudge)

---

## Wo ändere ich was?

| Änderung | Datei(en) | Zusätzlich |
|---|---|---|
| Neuer API-Endpoint (User-auth) | `backend/app/routers/{domain}.py` | Frontend `lib/api.ts` + `lib/types.ts` |
| Neuer Agent-Endpoint (agent-auth) | `backend/app/routers/agent_scoped.py` | TOOLS.md Template anpassen |
| Agent-Config-Feld | `backend/app/models/agent.py` + Alembic-Migration | `cli-bridge.py` Template-Context |
| Agent-Config-Content | `backend/templates/*.j2` | Reprovision der Agents nötig |
| Dispatch-Verhalten | `backend/app/services/dispatch.py` | Watchdog + Task Runner ggf. anpassen |
| Neue Task-Status / Workflow | `backend/app/models/task.py` + Routers + Frontend types | Watchdog + Task Lifecycle |
| Runtime-Wechsel pro Agent | `backend/app/services/agent_runtime_switch.py` (atomic) | Tests + UI-Modal in `RuntimeSwitchModal.tsx` |
| Runtime-Drift-Probing / -Intervall | `backend/app/services/runtime_watcher.py` (`settings.runtime_watcher_interval`/`_enabled`) | ADR-054 — 2-Probe-Confirm, `/runtimes/live-status` |
| Agent-Model-Sync nach Drift | `backend/app/services/runtime_propagation.py` (`docker restart`, kein respawn) | ADR-054 — Circuit-Breaker 3 Fehlversuche, Force-Route `POST /runtimes/db/{slug}/sync-agents` |
| Engine-Control (Autostart-Flag) | `backend/app/services/runtime_autostart.py` (SSH via `runtime_manager._ssh_run`) | ADR-057 — `runtimes.autostart_supported`/`autostart_flag_path`, `GET/POST /runtimes/db/{slug}/autostart`, `AutostartToggle.tsx` |
| Docker-Compose Image-Mapping | `backend/app/services/compose_renderer.py` (DB-driven) | `docker/docker-compose.agents.yml` ist generator-managed |
| Hermes Worker (host-side) | `scripts/hermes-bridge.py` + `~/.openclaw/agents/hermes/` | ADR-029 — eigene Bridge, NICHT cli-bridge.py |
| Per-Agent Timeout-Overrides | `agents.dispatch_config` JSON-Keys (`ack_timeout_minutes`, `idle_timeout_minutes`) + neue Alembic-Migration | Idempotente Migration analog zu 0096/0097; `task_runner._idle_threshold_for(agent)` liest 4-stufige Prio. Siehe ADR-031. |
| Silent-Abort-Block-Threshold | `agents.dispatch_config["stuck_block_minutes"]` (per-Agent) + `config.lifecycle_watchdog_enabled` (Kill-Switch) | `task_runner._stuck_block_threshold_for(agent, runtime)`: Override ist GEFLOORT (`max(role_idle, 20)` — Prime Directive, nie unter Recovery-Threshold). Nur cli-bridge (guard 0). Siehe ADR-046. |
| Docker-Agent-Image | `docker/mc-agent-base/{Dockerfile, entrypoint.sh, poll.sh}` | Rebuild mit `--no-cache`, Container recreate |
| omp-Harness-Image | `docker/omp-bridge/{Dockerfile, entrypoint.sh, bridge.py, omp-recycler.sh}` (`mc-omp-agent:latest`) | ADR-045 — `bridge.py --serve` Poll→omp→Lifecycle. Routing: `compose_renderer.pick_image_for_runtime` + `internal.build_runtime_env` (`omp`-Branch) + `agent_runtime_switch` (ready_signals). GATED build |
| tmux-Verhalten | `docker/mc-agent-base/entrypoint.sh` (`.tmux.conf` Write) | Rebuild |
| Frontend-Page | `frontend-v2/src/app/{page}/page.tsx` | Ggf. `lib/api.ts` + types |
| Browsebare Datei-Wurzel hinzufügen/ändern | `backend/app/services/fs_roots.py` (Registry, SSoT) | Nie `secrets`/Token-Config browsebar machen (ADR-040) |
| Datei-Zugriff (list/stat/stream) | `backend/app/services/fs_service.py` (einziger Containment-Guard) | Nie an `fs_service` vorbei os.listdir/open |
| Deliverable-Pfad-Auflösung | `backend/app/services/fs_service.py::resolve_deliverable` (runtime-aware) | `deliverable_fs_resolver` + `tasks.py` delegieren nur |
| Files-API / `/files`-Seite | `backend/app/routers/files.py` + `frontend-v2/src/app/files/page.tsx` | `api.files` Namespace + `file_index` (Accelerator) |
| Design-Token | `frontend-v2/src/styles/globals.css` (`@theme`) | — |

**Vor grösseren Änderungen:** `python3 tools/generate-code-map.py` + `docs/code-map.md` lesen (Dependency-Graph).

---

## Referenzen

- **Projekt-Regeln:** [`CLAUDE.md`](../CLAUDE.md) (Root)
- **Code Map (Dependencies):** [`docs/code-map.md`](code-map.md) — Auto-generiert via `tools/generate-code-map.py`
- **Agent State:** [`docs/agent-state.md`](agent-state.md) — Auto-generiert via `tools/generate-agent-map.py`
- **V2 Release Notes:** [`docs/mc-v2-release.md`](mc-v2-release.md)
- **V2 Design Spec:** `../MC-CLI-TMUX-PATCH/docs/superpowers/specs/2026-04-07-mc-v2-full-design.md`
- **Design Decisions:** [`docs/decisions/`](decisions/)

---

## Änderungshistorie (high-level)

- **2026-07-06** — **Solo-Capability-aware Recipe Switching (ADR-059):** Behebt den "engine unreachable"-Vorfall beim Recipe-Switch auf `@eugr/qwen3.6-35b-a3b-fp8` (tp=2 auf dem 1-GPU-Spark). `sparkrun_manager.list_recipes()` parst jetzt `tp`/`nodes` aus `sparkrun list` und berechnet `solo_capable` gegen die per `get_host_gpu_count()` (`nvidia-smi -L | wc -l`, host-scoped ADR-048) ermittelte reale GPU-Zahl. `switch_recipe()` bricht Multi-Node-Recipes VOR dem Evict ab (`runtime.recipe_switch_rejected`-Event) und injiziert `--tensor-parallel <host_gpu_count>` bei reinen TP-Overages (`build_launch_command(..., tp_override=...)`) statt sich auf `--solo` zu verlassen (das nur den Ray-Bootstrap steuert, nie den tp-Wert). Zweiter, unabhängiger Fix am selben Vorfall: `runtime_manager.start_runtime()` bekommt einen zweiten Post-Launch-Check `verify_spark_vllm_process_started` (`docker top`-Scan auf einen echten `vllm serve`-Prozess) — schliesst die Lücke, dass ein Container "running" sein kann (PID1 `sleep infinity`), während der injizierte vLLM-Prozess längst gecrasht ist, und sparkrun das per `--no-follow` nie meldet. Frontend `SparkRecipeSwitcher` zeigt tp/nodes-Badges und deaktiviert nicht-solo-fähige Recipes. Als Nebenbefund verifiziert: der Agent-Restart-Propagationspfad (`runtime_propagation.py`) kann strukturell nie einen sparkrun-Container treffen (Container-Name kommt nur vom Agent-Slug) — Assertion + Regressionstest als Tripwire; der `docker restart`, der beim Vorfall den sparkrun-Container zurücksetzte, kam vom manuellen `/runtimes/{id}/restart`-Button, nicht aus der Propagation. ADR-059.
- **2026-07-05** — **Engine Control v0: Autostart-Flag via SSH (ADR-057):** Erster Baustein von Cockpit v2 ("MC folgt der Engine" → "MC steuert die Engine"). Neue Spalten `runtimes.autostart_supported`/`autostart_flag_path` (Migration 0146, additive, Default aus) — Operator setzt sie zur Laufzeit via `PATCH /runtimes/db/{slug}` oder UI, nie geseeded. `services/runtime_autostart.py` führt `test -f`/`touch`/`rm -f` über den bestehenden `runtime_manager._ssh_run()` + Host-Registry-Resolver (`host_id` → `hosts`, ADR-048) aus — keine zweite SSH-Implementierung, kein separates Host/User-Feld pro Runtime. `GET/POST /runtimes/db/{slug}/autostart` (on-demand, nicht Teil des 90s-Watcher-Takts, ADR-054-Präzedenzfall): POST touched/entfernt die Flag-Datei und liest sie zur Verifikation zurück, emittiert `runtime.autostart_changed`; ein unerreichbarer Host liefert `enabled: null, reachable: false` bzw. bei POST einen 502 mit klarer Meldung statt Stacktrace. Frontend `AutostartToggle.tsx` auf der `/runtimes`-Karte (nur wenn `autostart_supported=true`): 3 Zustände an/aus/unbekannt, disabled bei unbekanntem Host, kein optimistisches UI. ADR-057.
- **2026-07-05** — **CLI-Tool-Updates aus User-Sicht (ADR-058, Migration 0147):** `docker/cli-versions.json` wird Single Source of Truth für die Soll-Versionen der drei Agent-CLIs (`openclaude`/`claude`/`omp`), gelesen von `build-agent-images.sh` (Build-Args) und `services/cli_versions.py` (Ist-Stand via OCI-Labels `mc.cli.name`/`mc.cli.version`). `services/cli_update_check.py` prüft periodisch (6h, npm/GitHub-Releases) auf neue Versionen, kein Auto-Update. Ein UI-Klick auf `/runtimes` → CLI-Tools löst `services/cli_update_runner.py` aus: Manifest bumpen → Build via `cli-bridge.py` `POST /agent-images/build` auf dem Host (Docker-Socket-Proxy-Regel `BUILD: 0`, ADR-047) → bei Fehlschlag Manifest-Rollback, bei Erfolg Rolling Recreate der betroffenen Harness-Agents (idle sofort, busy → `agents.pending_recreate`, abgearbeitet vom nächsten Runtime-Watcher-Tick — ADR-054-Propagationsmechanik wiederverwendet). Neue API `/api/v1/cli-tools`, neue Frontend-Sektion `CliToolsSection` auf `/runtimes`. Bewusst nicht v1: GHCR-Publish, Auto-Update-Policy, Changelog-Anzeige.
- **2026-07-05** — **Human-simulating E2E-Toggle (Migration 0142):** Auftragsmaske bekommt die Toggle-Pill »E2E test« (`tasks.e2e_test_required`) — nach Review-Approve geht der Task durchs bestehende `user_test`-Gate auch OHNE Subtasks/needs_browser; der Tester-Agent fährt echte User-Flows über den Playwright-MCP (`browser_navigate/click/type/snapshot`, Screenshots inline). Dabei zwei Altlasten gefixt: (1) **`handle_test_handoff` übergab den String `tester` an `find_agent_by_role`, das `role.value` aufruft → JEDER Test-Handoff crashte seit jeher still** (try/except-Warning), user_test-Tasks bekamen nie einen Tester — jetzt `AgentRole.TESTER` + Regression-Test; (2) die Tester-Directive (`_build_test_message`) hardcodierte den toten `dev-browser`-CLI-Heredoc — jetzt Playwright-MCP-Flow, konsistent mit SOUL.md. Fail-loud: explizit angefordertes E2E ohne verfügbaren Tester-Agent → `blocked` + Operator-Blocker-Kommentar statt stillem Skip (implizites Gate behält Legacy-Verhalten).
- **2026-07-05** — **GitHub-Verbindung als First-Class-Anschluss (ADR-055):** Neuer zentraler Resolver `services/github_config.py` löst Owner + Token auf (Vault-Keys `github_owner`/`github_token` > Env `GITHUB_OWNER`/`GH_TOKEN`, 30s-TTL-Cache, Invalidierung bei Vault-Writes) — die beim Import eingefrorene Modul-Konstante `git_service.GITHUB_OWNER` und das Einmal-Auth-Flag sind weg: `_ensure_git_auth` schreibt `~/.git-credentials` bei Token-Wechsel neu und injiziert den aufgelösten Token in jede `gh`/`git`-Subprozess-Env (UI-Rotation gilt sofort, ohne Neustart). Sichtbarkeit: `GET /repos/github-status[?probe=true]` (Quellen + Live-Check login/owner/rate-limit), `PUT /repos/github-config` (admin, Vault-Upsert; `""` löscht → Env-Fallback), Settings-Sektion **GitHub** (Statuskarte + Test connection), optionaler Connect-GitHub-Step im Setup-Wizard, Onboarding-Banner auf `/repos`, interaktive Owner/Token-Abfrage in `install.sh` (Token no-echo). Startup-Seed erweitert (`GITHUB_OWNER` → Vault, Cache-Priming für sync-Renderkontexte); `github_visibility_monitor` loopt jetzt statt beim Boot aufzugeben und aktiviert sich live, sobald ein Owner konfiguriert wird. Secrets-API invalidiert den Resolver-Cache bei `github_*`-Writes; Provider-Templates um `github` ergänzt. Doku: `docs/setup/github.md`. ADR-055.
- **2026-07-05** — **Runtime Watcher & Model-Propagation (ADR-054, supersedes D-22):** `services/runtime_watcher.py` singleton loop (90s default) probes enabled probeable runtimes (`vllm_docker`/`lmstudio`/`openai_compatible`/`unsloth`) via `/v1/models`, publishes live status to Redis (`mc:runtime-live:{slug}`), and confirms model drift with two consecutive identical probes before persisting `model_identifier` + invalidating the resolver cache + emitting `runtime.model_changed`. `services/runtime_propagation.py` syncs bound cli-bridge agents: idle agents get `sync_docker_agent_files()` + a plain `docker restart` (re-bootstrap, **not** `respawn_window_only` — that would keep the stale tmux env); busy agents stay `pending_runtime_sync` until the next watcher tick (Migration `0141`). Circuit breaker after 3 failed syncs (`agent.model_sync_failed`). New routes: `GET /runtimes/live-status`, `POST /runtimes/probe-endpoint` (wizard backend), `POST /runtimes/db/{slug}/sync-agents` (force), `GET /agents/{id}/runtime-switch-progress` (stepper: `rendering → restarting → waiting_healthy → done|rolled_back`). `/runtimes` cockpit: live-dot, "Engine serves" model, drift/pending badges, force sync, guided Add-Runtime wizard. omp provider renamed `qwen-spark` → `mc-openai`; seeds ship `model_identifier: null`. ADR-054.
- **2026-07-04** — **Einheitliche Repo-Auswahl in der Task-Maske (ADR-052):** `tasks.repo_id` (Migration 0139) — Ad-hoc-Aufträge wählen ihr Repo aus der Registry (Vorrang Task > Projekt > mc-workspace für Clone UND Regel-Injektion; Clone-Fehler blockt wie beim Projekt-Repo). `POST /repos/new` als einziger Neu-Anlage-Pfad (privat + Initial-Commit + registriert). `use_separate_repo` deprecated: registriert sein Repo jetzt mit (keine Schatten-Repos). `git-info` liefert `repo_id`+`has_rules` → Regeln-Badge in der Maske; Projekt ohne Repo kann bestehendes Registry-Repo verknüpfen. Scheduler reicht `project_id`/`repo_id` aus Job-Templates durch (verwarf sie vorher still). ADR-052.
- **2026-07-04** — **Loops L1 (ADR-051):** Ergebnisgesteuerte Task-Schleifen als **Meta-Controller über normale Tasks** — pro Runde erzeugt `services/loop_runner.py` (Singleton, 30s-Tick, Per-Cycle-Redis-Lock) einen normalen Parent-Task (Board-Lead-first via `create_task_internal`) und wertet dessen Ausgang aus: Circuit-Breaker (Default 2 Fehlrunden → paused + `loop_gate`-Approval mit Telegram-Quick-Resolve), Stop-Bedingungen (max_rounds, max_duration, »BACKLOG LEER«-Reflexion), optionales Human-Gate (`human_every_n_rounds`, Default 0 — Marks Entscheid: Gates nur bei Problemen/Merges), sonst nächste Runde mit Report-Kontinuität (letzte 3 Runden-Reports im Brief). Tabellen `loops`+`loop_rounds` (Migration 0138), API `/api/v1/loops` (CRUD + start/pause/stop, 1 aktiver Loop pro Board), Frontend `/loops`. Task-Delete-Endpoints lösen die neuen nullable Loop-FKs. L2 (Telegram-Reports, Schedule-Trigger, project/tag-Backlogs), L3 (Token-Budget via cost_collector-Revival), L4 (Lessons) folgen. ADR-051.
- **2026-07-04** — **Repos Registry (ADR-050):** GitHub-Repos werden first-class: neue Tabelle `repos` (Migration 0137, `full_name` kanonisch `owner/name`, `rules_md`, `source`, `is_active`) + `projects.repo_id` FK mit Backfill aus den Legacy-Strings. **Per-Repo-Arbeitsregeln** fliessen automatisch in jede Worker-Directive (`task_context_builder` löst Repo via `repo_id`/Legacy-Name auf → `dispatch_message_builder` hängt „Repository-Arbeitsregeln — BINDEND" an die Git-Sektion). Legacy-Sync-Kontrakt: `services/repo_registry.apply_repo_link` hält `github_repo_url/_name` konsistent — alle Clone-/PR-/Merge-Flows unverändert. API `/api/v1/repos` (CRUD, `gh repo list`-Import, Sync, Link/Unlink; Delete löscht NIE auf GitHub), Frontend-Seite `/repos` (Liste, Regeln-Editor, Import-Dialog). Bugfix nebenbei: `init-repo` schrieb `github_repo_name` ohne Owner-Präfix → brach `gh --repo`-Aufrufe. ADR-050.
- **2026-07-02** — **Host-Registry (ADR-048):** Generische Multi-Host Control-Plane statt neuer runtime_type pro Box. Neue Tabelle `hosts` (Migration 0133, kind `ssh`/`flask_wol`/`local`) + `runtimes.host_id` FK; `services/host_resolver.py` mit 4-stufiger Back-Compat-Kette (host_id → Legacy `runtime.host` → `settings.dgx_ssh_host` → None); `runtime_manager` arbeitet nur noch mit `ResolvedHost` (`_ssh_run` host-parametrisiert, `get_spark_metrics()` → `get_host_metrics(host)`, Eviction host-scoped); idempotenter Bootstrap-Seed (dgx-spark/porsche), Fresh-Install ohne GPU-Host = 0 Hosts, 0 Fehler; `routers/hosts.py` CRUD+Metrics + Spark-Metrics-Alias; Frontend `/runtimes` mit Hosts-Sektion + `HostMetricsBar`. Legacy-Runtime-Felder deprecated, Welle 3 (Placement/Scheduler) bewusst geparkt. ADR-048.
- **2026-07-02** — **Docker-Socket-Proxy (ADR-047):** Backend spricht Docker nur noch via `DOCKER_HOST=tcp://docker-socket-proxy:2375` (tecnativa/docker-socket-proxy, API-Whitelist: containers/images/networks/volumes/exec/info+POST; build/swarm/system geblockt). Socket-Mount + `group_add: "0"` am Backend entfernt. Ausserdem: Compose-Profiles `voice` (livekit, voice-worker) + `browser` (mc-playwright, playwright-mcp) — Default-Boot ist der Lean-Core (6 Services); omp-Bridge heartbeatet (Daemon-Thread, working/idle aus Task-Lock); `approvals.agent_id` nullable + Board-Archivierung gibt Slug frei (Migration 0132); `scripts/demo-seed.py`.

- **2026-07-02** — **Vertical-Module (ADR-044)**. Neues `app/verticals/`-Paket mit pkgutil-Discovery (`register_all`) + Hook-Registry (`app/verticals/hooks.py`) als einziger Vertical→Core-Kopplung. Erstes Vertical `news_studio` extrahiert (7 Router + content_agent-Callback + 10 Services; frontend `src/verticals/news-studio/` mit eigenen types/api, Sidebar-Gating via `src/lib/verticals.ts`). Models+Migrationen bleiben im Core. Public-Release strippt das Vertical (release/internal-paths.txt + Flag-Flip). Verifiziert: Boot mit/ohne Paket (416/349 Pfade), Export-Backend bootet, Next-Build des Exports grün, Backend 2518 + Frontend 93 grün. ADR-044.
- **2026-07-02** — **Open-Source-Release-Vorbereitung (ADR-043)**. Fresh-History-Release-Prozess via `scripts/release-public.sh` (Interna-Strip + Zero-Grep-Gate + Gitleaks-Gate). Identitäts-/Pfad-Vertrag über Env: `OPERATOR_NAME` (Templates rendern `{{ operator_name }}`), `GITHUB_OWNER`, `TELEGRAM_CHAT_ID`, `NEWSLETTER_BRAND`, `NEWS_REPO_PATH`, `HOST_SSH_USER`, `MC_OWNED_REPO_PREFIXES`, `MC_REPO_PATH`, `HOST_UID`, `LIVEKIT_NODE_IP`; Host-Pfade via `HOME_HOST` (`settings.home_host`), Repo-Pfad via `settings.mc_repo_path` (Force-Recreate/Runtime-Switch/Compose-Renderer). Maschinen-Mounts → `docker-compose.override.yml` (Beispieldatei), Caddy shipped nur `:80` (TLS via `caddy/Caddyfile.tls.example`), `pg_hba.conf` auf scram-sha-256, setup.sh GNU/BSD-portabel (schreibt + backfillt HOST_UID/MC_REPO_PATH), echtes `DB_PASSWORD` aus `tools/generate-agent-map.py` entfernt. LICENSE (Apache-2.0) + CONTRIBUTING + SECURITY + englisches README. Migration 0095 seedet Hermes-Workspace via `_home()`. Zeitbomben-Fix test_model_prices. Backend 2467 grün, Frontend 93 grün. ADR-043.
- **2026-07-01** — **omp Runtime-Typ — Clean-Stream Headless Agent (ADR-045)**. Dritter Harness-Image-Typ `mc-omp-agent:latest` neben `mc-claude-agent` (native claude) und `mc-agent-base` (openclaude). Ein Agent (zuerst Sparky) kann auf `runtime_type = "omp"` geswitcht werden: erscheint in `/runtimes`, ist über den Standard-`switch_agent_runtime`-Pfad switchbar und läuft headless über `docker/omp-bridge/bridge.py --serve` (persistenter Poll→omp→Lifecycle-Treiber, `omp -p --mode json` als kurzlebiger Subprozess). Schliesst die Silent-Abort-Lücke: der deterministische NDJSON-Klassifikator (`classify`/`decide_lifecycle`) mappt jeden Lauf terminal auf `mc ack` / `mc finish` / `mc blocked` — kein Task bleibt `in_progress`. **Routing (kein Token-Duplikat):** `compose_renderer.pick_image_for_runtime` → `mc-omp-agent:latest`; `internal.build_runtime_env` `omp`-Branch → `OPENAI_BASE_URL`+`OPENAI_MODEL` (keine anthropic-Tokens); `docker_agent_sync` unverändert (non-anthropic Slug `omp-qwen` nimmt den OpenAI-Zweig). **Readiness:** `wait_for_agent_healthy(ready_signals=("OMP_BRIDGE_READY",))` scrapt die Window-0-Pane auf beiden Switch-Pfaden (auch cross-image, wo `respawn_mode=False` sonst false-positiv `docker inspect …==running` liefern würde). **Config:** Entrypoint rendert omp's `models.yml` (`qwen-spark`-Provider, `auth: none`) aus `OPENAI_BASE_URL`/`OPENAI_MODEL`. Registrierung: idempotenter Seed `omp-qwen` in `backend/config/runtimes.json` + `docker/omp-bridge/register-omp-runtime.sh`. Alle Produktions-Aktionen (Image-Build, Registrierung, Switch) GATED. Tests: `backend/tests/test_omp_runtime.py` (12) + `docker/omp-bridge/tests/test_serve_loop.py` (7) + `test_bridge.py` Golden (17). Design: `docs/plans/omp-runtime-design.md`.
- **2026-07-01** — **Lifecycle Safety Watchdog — Silent-Abort Auto-Block (ADR-046)**. Schliesst den Silent-Abort-Bug: ein Agent ackt eine Task (`in_progress`, `ack_at` gesetzt) und verstummt dann, ohne je einen terminalen `PATCH` (review/blocked/failed) zu senden → Task hängt für immer `in_progress`. **Neuer Check** `task_runner._check_stuck_in_progress` (peer von `_check_stale_in_progress`, läuft im 60s-Tick DANACH, damit Tiered-Recovery zuerst greift). **Prime-Directive-safe by design:** (guard 0) nur `agent_runtime=='cli-bridge'` — die einzige Runtime, die `last_task_activity_at` *während* der Arbeit stempelt (poll.sh Bug-13); host/manual/claude-code hard-skip. Liveness-Delta: `last_seen_at` frisch (Wrapper lebt) UND `last_task_activity_at` stale über `stuck_block_minutes` (runtime-aware Default 25min claude / 45min slow-local, per-Agent-Override GEFLOORT auf `max(role_idle, 20)`). Korroboration (kein Agent-TaskComment im Fenster) + staged (tick 1 nudge, tick 2+ block über `≥2`-Tick-Redis-Counter). **Block-Aktion:** `apply_terminal_unassign(…, "blocked")` (assignment bleibt → resumable, Lock frei, `run_state='blocked'`) + `blocker_decision`-Approval (Telegram-Push) + `emit_event(task.status_changed, severity=warning)`. Idempotent via `RedisKeys.task_runner_stuck_block*` (24h) + DB-Fallback (pending-Approval-Recheck). Kill-Switch `config.lifecycle_watchdog_enabled`. host-Abdeckung ist bewusst deferred (braucht erst den Bug-13-Working-Heartbeat in `boss-host/poll.sh`). Files: `services/task_runner.py`, `redis_client.py`, `config.py`, `tests/test_stuck_in_progress_watchdog.py` (24 Tests, inkl. FP-Regression host/slow/healthy/dead-process). ADR-046.
- **2026-06-24** — **`unsloth_porsche` — power-managed Runtime (PORSCHE) + Wake-on-LAN + Runtime-Readiness-Dispatch-Gate (ADR-042)**. PORSCHE (Windows-Box, lokaler unsloth-OpenAI-Server) wird eine vollwertige LLM-Runtime, an die Agenten sich per `runtime_id` binden — wie an den DGX, nur dass die Box im Leerlauf **schläft**. **Eigener `runtime_type` statt DGX-`unsloth`-Branch-Erweiterung** (null DGX-Regressionsrisiko). **Control-Plane:** Flask `:5555` (`POST /powershell`, `GET /health`) + OpenAI-Health `/v1/models` statt SSH/tmux — neue Helper `_porsche_reachable`/`_porsche_powershell`/`_porsche_default_stop_command` + je ein `unsloth_porsche`-Branch in `get_runtime_state`/`start_runtime`/`stop_runtime`/`restart_runtime` (`services/runtime_manager.py`). **Power-Lifecycle bedarfsgesteuert:** WoL weckt nur die Box (billig), das Modell lädt erst on demand via Start (Warmup ~1–3 Min) → GPU/VRAM/Strom nur bei Nutzung. State-Mapping: `:5555` aus → `stopped`/`asleep`, da-aber-`/v1`≠200 → `stopped`/`booted_no_model`, `/v1/models`=200 → `ready`/`serving`. **Neue Runtime-Felder** (Migration 0130, alle nullable/default-off): `control_url`, `wol_mac_address`, `power_managed` — auf `Runtime`-Model + `RuntimeCreate`/`RuntimeUpdate` + `to_registry_dict`; Seed in `runtimes.json` als `enabled=false` (TODO-Platzhalter für Port/Modell/`launch_command`). **Wake-Flow** (Backend kann kein L2-Broadcast aus Docker): `POST /api/v1/runtimes/{id}/wake` → `runtime_manager.wake_runtime()` schreibt Trigger-Datei `~/.mc/wake-requests/<slug>.request.json` (`{slug, mac, ip, broadcast, requested_at}`), host-seitiger launchd-Watcher ruft `~/.claude/skills/wake-porsche/wake_porsche.py` auf; Endpoint hart auf `power_managed` gegated (400/404). **Runtime-Readiness-Gate** (`services/runtime_readiness.py`): konsultiert in `operations.check_dispatch_allowed` (Schritt 3.5, neuer optionaler `session`-Param; alle 6 Push-Aufrufstellen/5 Dateien liefern ihn) + `routers/agents.py::agent_poll` (nur frischer Inbox-Claim). **Greift ausschliesslich für `power_managed`-gebundene Agenten** — `runtime_id` NULL / nicht-power-managed / Kill-Switch `enable_runtime_readiness_gate=false` → sofortiger früher Return; jeder andere Agent (24/7 cli-bridge, host, DGX, cloud) läuft den unveränderten Pfad. Readiness ~15 s Redis-gecacht (kein `:5555`-Hämmern), **jeder Fehler fällt OPEN** (Gate-Bug kann die Fleet nie stallen). Schlafende Box → Task bleibt geparkt (inbox, `dispatched_at` ungesetzt) bis `ready`. **Default manuelles Wecken**; Auto-Wake-on-dispatch + periodisches Background-Probing bewusst verworfen/deferred. **Security:** Runtime-DB-Writes (`launch_command`/`control_url` → PowerShell/POST) sind admin-only (`require_role(Role.ADMIN)`, vorher `require_user`) + `control_url` auf `http(s)://` validiert → RCE/SSRF-Vektor geschlossen; `:5555`-Auth bleibt offener PORSCHE-seitiger Betriebspunkt (Firewall/Shared-Secret empfohlen). **Config:** `porsche_lan_ip`/`porsche_mac`/`porsche_broadcast`/`porsche_control_url`/`wake_request_dir`/`enable_runtime_readiness_gate`/`runtime_readiness_cache_ttl`. **Migration:** 0130 (3 Runtime-Spalten). **Tests:** `test_runtime_manager_porsche.py`, `test_runtime_readiness_gate.py`. Design-Doc: `docs/plans/2026-06-24-porsche-unsloth-runtime-design.md`.
- **2026-06-18** — **MC Files System — portabler, runtime-aware Datei-Zugriff (ADR-040)**. Neue globale `/files`-Seite + portable Filesystem-Schicht, die den Mobile-Bug behebt (Ordner-Icon öffnete `open -R` auf dem Mac Mini, nie auf dem Handy) und MC reusable macht. **Backend:** `services/fs_roots.py` (SSoT der browsebaren `~/.mc`-Wurzeln; `secrets`/Token-Config/`browser-profiles`/`logs`/`backups` hart ausgeschlossen), `services/fs_service.py` (EIN sandboxed Zugriff mit einem Containment-Guard `safe_join` gegen `..`/Symlink-Escape/NUL + runtime-aware `resolve_deliverable`, der die zwei Resolver-Kopien aus `deliverable_fs_resolver`+`tasks.py` konsolidiert und die `.mc-deliverables`-Hyphen-Landmine droppt), `models/file_index.py` + `services/file_indexer.py` (capture-at-write + Background-Walk; nur Listing/Such-Accelerator, Bytes streamen immer live), stabile `agents.slug`-Spalte (before_insert, rename-fest), `routers/files.py` (`/api/v1/files/roots|list|search|content|meta|open|reindex`; native open capability-detected via TCP-Probe `:8765`). **Portabilität:** `HOME_HOST→settings.home_host` (Default `Path.home()`, fail-loud-Warnung), `PUBLIC_HOST`/`EXTRA_CORS_ORIGINS` statt hartkodierter Tailscale-IP `<tailscale-ip>` (CORS + 2 Telegram-Phone-Links). **Frontend:** `/files`-Seite (Root-Selektor + Browser-Tabelle + Preview-Panel), `api.files` Namespace, `FilePreview` rendert Markdown rich + Download überall, `isHostPath()`-Rätselraten raus → Backend-Flags `reachable`/`native_open_available` (Finder-Button versteckt sich auf Mobile). **Bewusst aufgeschoben:** physische Deliverable-Layout-Normalisierung (Host-Worker nach `<slug>/<task_id>/`) — berührt High-Risk-Dispatch + `mc` CLI + Datei-Migration; der Resolver behandelt beide Layouts bereits uniform. Spec: `docs/superpowers/specs/2026-06-18-mc-files-system-design.md`. Migration 0129 (file_index + agents.slug).
- **2026-05-17** — **OpenClaw Gateway Sunset (v0.9, ADR-039)**. Sechs-Wochen-Kriechmigration plus Vier-Phasen-Final-Sweep entfernen den OpenClaw Gateway als Runtime-Komponente, Code-Pfad, DB-Schema, Frontend-Konzept und Host-Service. **Scope:** Phase 28 (Henry-Agent gelöscht, Boss übernimmt Board-Lead via `is_board_lead`-Flag; Active-Task-Migration via `mc_henry_sunset.py` Skript; Migrations 0121 FK-swap + 0122 reassign+delete), Phase 29 (~2700 LOC Backend-Code raus — `services/openclaw_rpc.py`, `services/gateway_sync.py`, `services/gateway_secrets_sync.py`, `services/telegram.py` Gateway-Pfad, `services/gateway_client.py`, `routers/gateway.py`; Discord-Bot-Endpoints leben in eigenem `routers/discord.py` (Plan 29-01); ~200 Call-Sites refactored in `dispatch.py`, `dispatch_delivery.py`, `task_lifecycle.py`, `task_runner.py`, `watchdog/*`, `routers/agents.py`, `agent_scoped.py`, `tasks.py`, `skills.py`, `models.py`, `research.py`, `content.py`, `chat.py`, `approvals.py`, `system.py`, `operations.py`, `cost_collector.py`, `meeting_service.py`, `telegram_bot.py`, `workflow_service.py`, `provisioning.py`; `OPENCLAW_WS_URL` + `OPENCLAW_TOKEN` aus `config.py` raus; `watchdog/task_runner` nutzt DB+Redis statt `sessions_list()` für Stale-Detection), Phase 30 (Alembic 0123 droppt `gateways` Table + `agents.gateway_id`/`agents.gateway_agent_id`/`boards.gateway_id`; neue `discord_config` Tabelle (single-row, guild_id/category_id/bot_configured) mit CHECK-Constraint + Daten-Migration aus `gateways.discord_*`; `agent_runtime`-Enum verliert `"openclaw"`. **`agents.workspace_path` BLEIBT** — Phase 14 / ADR-022 Repurpose auf agent-home-path (`~/.mc/workspaces/<slug>`), NICHT Gateway-VPS-Path wie SQLModel-Docstring fälschlicherweise behauptete), Phase 31 (`/skills` Page neu mit 3 lokalen Tabs — Local Skills + CLI Plugins + MCP Servers; `/settings` OpenClaw-Provider-Block + Sync-to-Gateway-Button raus; `/agents/[id]` Provision/Reset/Sync-Buttons strikt auf `agent_runtime === 'cli-bridge'` gegated; `lib/types.ts` + `lib/api.ts` Gateway-Interfaces (`Gateway`, `OpenClawHealth`, `OpenClawModel`, `OpenClawSyncResult`, `GatewaySession`) + `api.gateways.*` / `api.openclaw.*` / `api.secrets.syncToGateway()` raus; Workflows migriert auf `api.discord.*`; Host-Filesystem-Cleanup `~/.openclaw/{identity,logs,cron,credentials,extensions,plugin-store,delivery-queue,exec-approvals.json,restart-sentinel.json}` archiviert als `~/.openclaw-archive-2026-05-17.tar.gz` + gelöscht, Symlinks `~/.openclaw/{agents,skills,plugins,mcp-servers}` zu `~/.mc/` BLEIBEN — aktiv genutzt von cli-bridge Agents). **Erhalten geblieben:** `GatewayMessage` / `GatewayMessagePart` Types in `types.ts` (Anthropic chat-history shapes, historisch fehlbenannt — Rename in Follow-up), `agents.workspace_path` Spalte + Field. **Multi-Agent-Stack läuft jetzt direkt:** Host-Boss (native `claude` Binary + Anthropic OAuth), 9 Docker-Agents (`mc-claude-agent:latest` Image), Sparky (`openclaude` mit LM Studio / Ollama Cloud), Hermes (host tmux mit vLLM Qwen3.6-35B), Jarvis (LiveKit / xAI Grok Voice). Kein WebSocket-RPC, kein Port :18789, keine `~/.openclaw/identity` Keys mehr im aktiven Pfad. ADR-039 von Proposed → Accepted. Plan-Trace: 28-01..03, 29-01..10, 30-01..03, 31-01..06.
- **2026-05-16** — **Voice-Agent → Jarvis Rename (ADR-038)**. Der xAI-Grok-betriebene Concierge-Agent hiess seit seiner Erstellung "Voice" — derselbe Begriff wie die LiveKit-voice-Infrastruktur darunter. Jeder Code-Treffer für "Voice" war ambig: Persona, Agent-Row, oder Infra-Schicht? Rename räumt das auf. **Scope:** Persona "Voice" → "Jarvis" überall wo die Identität gemeint ist; LiveKit/Worker/Routes (`voice-worker`, `/voice/*`, `VoiceWidget`, Redis-Channel `voice:graph-highlight`) bleiben "voice" weil sie die Infra beschreiben. **Migration 0120** macht atomar `UPDATE agents SET name='Jarvis' WHERE id='156b915b-…' AND name='Voice'` + rewrite von 5 historischen `activity_events.title`-Einträgen (vom Operator explizit gewünscht, historische Genauigkeit weicht Operator-Klarheit). **Env-var rename** `VOICE_AGENT_TOKEN` → `JARVIS_AGENT_TOKEN` mit `os.environ.get("JARVIS_AGENT_TOKEN") or os.environ.get("VOICE_AGENT_TOKEN")` Fallback in `voice_worker/mc_client.py` für Bootstrap-Phasen. **System-Prompt** in `voice_worker/main.py:JARVIS_INSTRUCTIONS` lehrt Grok seinen neuen Namen + Anti-Confusion-Note ("wenn jemand 'Voice' sagt: alte Bezeichnung, du heisst jetzt Jarvis"). **Test-Fixtures** in 4 Files (~14 Replacements) + 1 Assertion (`requested_by` slug von "voice" → "jarvis"). 47/47 Tests grün nach Update. **Live-Verify:** voice-worker rebuild + force-recreate, `GET /api/v1/agent/me` → `name='Jarvis'`. **Migrations:** 0120 (rename agent + activity_events). **Touch-Points dokumentiert:** Backend-Migration, voice_worker code, Test-Fixtures, .env + compose, Docs (ARCHITECTURE.md, agent-state.md). LiveKit room-naming + Frontend VoiceProvider/-Widget bewusst unberührt.
- **2026-05-16** — Drei strukturelle Fixes nach DNA-PDF + qwen-runtime + dispatch-Race Vorfällen (ADR-035 + ADR-036 + ADR-037). **ADR-035 `dispatch_attempt_id` Audit-Trail** — Migration 0116 legt `task_attempt_audit` Tabelle an; `dispatch_attempt_audit.set_/clear_dispatch_attempt_id()` Helper sind ab sofort die einzige Schreibstelle für `tasks.dispatch_attempt_id` (12 Caller in 5 Services + 7 Routern migriert). `set_(only_if_null=True)` macht ein konditionales `UPDATE … WHERE dispatch_attempt_id IS NULL` — first-writer-wins, schliesst die `auto_dispatch_task` ↔ `/agent/me/poll` Race im git-clone-Fenster. Forensik ist jetzt eine SQL-Query statt 30 min Code-Walkthrough. Auslöser: 2026-05-15 Researcher-/Wetter-Staufen-Vorfall mit silent attempt_id-Rotation. **ADR-036 Runtime `launch_command`** — Erweitert ADR-028: neue nullable Spalte `runtimes.launch_command` (Migration 0117) + Path-A/B/C Logik in `start_runtime()`: existiert der Container → `docker start`, sonst SSH `bash -lc <launch_command>` (detached via nohup, `shlex.quote` für Shell-Injection-Schutz). Migration 0118 seedt `qwen-general` mit dem live-verifizierten `uvx sparkrun run @official/qwen3.6-35b-a3b-fp8-vllm --solo --no-rm --ensure --no-follow --label mc.runtime.slug=qwen-general` Aufruf (idempotent `WHERE launch_command IS NULL`). Schliesst den `--rm`-induzierten Cleanup-Bug, in dem `/runtimes` Start auf einen längst entfernten Container 404'te. **Live-Verify:** sparkrun_1299888bb0f6_solo Up nach 5 min Build + 3 min Warmup, /v1/models HTTP 200, Researcher-Wetter-Task in 82 s mit qwen3.6 (44× schneller als Nemotron). **Vault-Watcher Trash-Exclusion** — `_trash/` zu `vault_constants.EXCLUDED_PREFIXES` hinzugefügt. Vorher: nach soft-delete indexierte der watcher die Datei unter ihrem neuen `_trash/<ts>-foo.md` Pfad → die "gelöschte" Note tauchte sofort wieder in der List-View auf, Klick darauf 404'te (GET endpoint refused `_trash/`-Pfade). 4 leaked Index-Einträge im Live-System per SQL-DELETE bereinigt. **ADR-037 `mc finish` Preflight + Idempotency** — CLI-Wrapper macht jetzt explizite GET-Pre-Checks (Status / Checklist / recent self-reflection / literal `\n` shell-escape) BEVOR der Reflexions-POST raus geht. Idempotenter No-op wenn Task schon im Ziel-Status; 5-min Dedup-Window für recent self-reflections; Recovery-Hint bei post-POST-PATCH-Fail (`# Reflexion gepostet, retry mit mc done`). Schliesst den DNA-PDF-Vorfall in dem Researcher 3 Reflektionen in 53 s postete weil `mc finish` POST-then-pray war und jeder PATCH-422 (offene Checklist) zu einem retry mit weiterem Comment führte. **Live-Verify:** "LLM Modelle für DGX Spark"-Task lief in 358 s mit **1** Reflection (statt 3 wie beim DNA-PDF-Vorfall ohne Fix). Image-Rebuild + force-recreate aller 8 cli-bridge Agents deployed. **Migrations:** 0116 (task_attempt_audit), 0117 (runtimes.launch_command), 0118 (qwen-general seed). **Tests-Delta:** +6 Backend (`test_dispatch_attempt_audit.py`) + +14 Backend (`test_runtimes_endpoints.py` für die DB-aware /start/stop/restart/health Endpoints, follow-up zu ADR-028) + +8 Backend (`test_runtime_launch_command.py`) + +1 Backend (`test_vault_watcher.py::test_trash_paths_not_reindexed`) + +19 mc-CLI (`tests/test_finish_preflight.py`).
- **2026-05-15** — Vault as Source of Truth live auf main (Merge `0b35ed83`, 102 Commits aus `feature/vault-memory-foundation`). **M.1-M.5 + M.4 Graph + Boss/Voice Rollout** — Markdown-Vault unter `~/.mc/vault/` ist jetzt Source of Truth für Lessons/Decisions/Knowledge/Concepts; BoardMemory-Schreibpfad deprecated. **Agent-Coverage:** 8 docker cli-bridge Agents (Sparky/Davinci/Rex/FreeCode/Researcher/Tester/Deployer/Shakespeare) bekommen `/vault:rw` Mount + AGENT_VAULT_PATH env + Vault-Section in TOOLS.md + vault:read/write Scopes (M.3 SQL UPDATE). Boss (host-runtime, native claude CLI) bekommt host-path agent.env + regenerated TOOLS.md mit `runtime="host"` Phrasierung. Voice (host-runtime, xAI Grok worker) nutzt `voice_worker/mc_client.py:vault_*` Function-Tools. Henry bewusst draussen (OpenClaw Council Gateway, nicht MC-orchestrierter Worker). **Alembic 0114** codifiziert Boss + Voice Scope-Grants idempotent. **Backend-Services:** `vault_index` (SQLite FTS5 mit auto-rebuild on schema migration), `vault_compactor`, `vault_watcher`, `vault_embeddings` (Spark DGX Qwen3.6 → Qdrant), `vault_activity` (Redis heatmap), `vault_git`, `vault_similarity_edges` (W3-A), `vault_wikilink_backfill` (W3-B LLM via Spark), `vault_cleanup`. **Frontend `/memory`:** VaultMemoryPage mit Liste + Graph Tabs. `MemoryGraph2D` (react-force-graph-2d) als Obsidian-style Konstellation mit `nodeRadiusFromLinkCount` 2-18px Hub-Hierarchie + charge=-300/linkDistance=25/forceX(0).strength(0.12) + forceY(0).strength(0.12) für sphärische Form. **W3-C `related_notes min_length=2 → 0` relaxed** — die erste Note in einem neuen Vault-Bereich hat legitimerweise keine Nachbarn, der Wikilink-Backfill-Job verknüpft Orphans retroaktiv. **W4** redirected `auto_memory.record_task_completion()` von BoardMemory zu TaskComments (audit-trail separation). **Lessons learned (in ADR-034 dokumentiert):** (1) `d3-force.forceCenter.strength()` ist ein silent no-op — sphärische Layouts brauchen `forceX(0)/forceY(0).strength(...)` explizit. (2) `tools_md_builder.py` braucht runtime-Awareness damit host-Agents nicht "im Container" Doku bekommen. **Migrations:** Alembic 0112 (board_memory → vault cutover, 881 rows, id-Backfill, 884 Phase-7-Legacy archiviert in `~/.mc/vault.phase7-pre-m2-20260515-000723`) + Alembic 0114 (Boss + Voice scope grants). **ADR-034 Status:** Proposed → Accepted (2026-05-15). **Tests:** 1899+ grün, 7 stale wegen Live-Activation (compose_renderer_vault x4, vault_e2e_m1 x3).
- **2026-05-13** — Bug 14 + Bug 15 fix: openclaude end-marker skip + recover_task paste-Pfad. **Bug 14:** `paste_and_submit` in `docker/shared/poll.sh` schickte den Bracketed-Paste-End-Marker `\e[201~` pauschal nach jedem `tmux paste-buffer`. claude-cli braucht den Marker (sonst bleibt der pty im paste-mode haengen), openclaude bricht daran (zeigt ihn als Literal-Text + verschluckt das Submit). Live-Symptom: Sparky stand stundenlang am `❯` prompt obwohl poll.sh "success" loggte. Neue Lib `docker/{mc-agent-base,mc-claude-agent}/lib/ui-detect.sh` mit `detect_pane_ui()` Heuristik (claude-cli `╭─` vs openclaude `❯`/`bypass permissions` footer) — gecached in globaler `PANE_UI_DETECTED`. `wait_for_clean_prompt` setzt die Variable bei jedem positiven Match. `paste_and_submit` skipt den End-Marker wenn `PANE_UI_DETECTED=openclaude`. Fail-open path probt zusaetzlich nochmal direkt vor dem paste. Default bei unbekannt: senden (safe fuer claude-cli-Mehrheit). 7-case bash smoke + pytest wrapper (`backend/tests/test_ui_detect.{sh,py}`). **Bug 15 (live entdeckt):** `recover_task` ruft `run_task` mit der Recovery-Response, aber Commit `35dc7b16` (2026-05-03, "session restart fix") hatte einen frueh-`return` bei `task.status=in_progress` eingebaut — der gesamte paste-Step wurde uebersprungen. Effekt: bei jedem Container-Recreate sah Sparky/FreeCode den prompt nie. Fix: `recover_task` setzt jetzt `IS_RECOVERY_DISPATCH=true`; `run_task` skipped bei dieser env-var nur den `/clear` aber faellt durch zum paste-Pfad. Beide Bugs zusammen Live-verifiziert: nach `bash scripts/build-agent-images.sh openclaude` + force-recreate ist Sparky innerhalb 20s an Voice-Foundation Task am Cooken (`status=working`, `current_task_id=c9fbe9cb...`, Window 0 zeigt Code-Reading Output). Follow-Up Bug 16 (low prio): `verify_paste_landed` capture-pane `-S -100` ist fuer >80-Zeilen-Dispatch-Prompts zu klein → false-negative WARN-Logs, paste-Workflow laeuft trotzdem.
- **2026-05-13** — Bug 2 refined + Bug 13 (NEU) + wait_for_clean_prompt openclaude-tolerance (Bug 14 prep). **Bug 2 refined:** ursprünglicher Fix (heartbeat setzt pauschal status=working bei active task) maskierte echte Inaktivitaet — Sparky stand am ❯ prompt aber DB zeigte working+last_task_activity_at=now. Jetzt: `agent.current_task_id` wird self-healed aus Task-Tabelle (Drift-Fix bleibt), aber `status`/`run_state`/`last_task_activity_at` folgen dem Payload — `status=working` nur wenn poll.sh wirklich "working" sendet. Der Operator sieht damit Wahrheit: `status=idle + current_task_id=xxx` = "Task assigned aber Agent nicht aktiv". **Bug 13 (NEU):** poll.sh main-loop heartbeat sendete bisher pauschal "idle", auch wenn claude im Cook ist. Jetzt: heartbeat verwendet `detect_turn_state` aus lib/turn-state.sh — sendet "working" nur wenn pane working-glyphs zeigt. Bug 2 + Bug 13 zusammen ergeben akkurate Live-Sicht ohne Maskierung. **`wait_for_clean_prompt` openclaude-tolerance:** akzeptiert jetzt auch `❯`-prompt + `bypass permissions` footer (openclaude pattern), nicht nur claude-cli's `╭─` box-glyphs. Bereitet Bug 14 fix vor (end-marker skip fuer openclaude). 8 heartbeat-Tests gruen.
- **2026-05-13** — Bug 12 fix (follow-up zu Bug 10): post-Image-Rebuild zeigte sich, dass `paste_and_submit` return 1 (Bug 10 fix) das poll.sh komplett killte weil `set -euo pipefail` jeden non-zero exit propagiert. Sparky's poll.sh crashed → entrypoint restartete den Loop → race-condition ob paste in der Zwischenzeit doch landete. Plus: verify-Heuristik gab false-negatives wenn openclaude den Paste 2-3s verzoegert rendert. Fix in 3 Teilen: (a) `paste_and_submit` callers in `run_task` + `deliver_comments` handlen jetzt explizit den return-code (`if ! paste_and_submit ...; then log WARNING; fi`) statt set-e-kill; (b) `verify_paste_landed` macht jetzt internal probe-loop mit `PASTE_PROBE_ATTEMPTS` (default 3) × `PASTE_PROBE_INTERVAL_SEC` (default 1s) capture-pane probes — reduziert false-negatives bei verzoegertem Rendering; (c) progressive fingerprint-Verkuerzung — wenn full 40-char fingerprint missed, probiert die Heuristik 50% + 25% prefix (faengt claude's text-wrap und box-border-injection ab). 2 neue bash test cases (9 von 9 gruen). Greift bei naechstem Image-Rebuild. Live-verifiziert nach Container-Recreate: Sparky cookt sauber an Voice-Foundation ohne paste-failures.
- **2026-05-13** — Bug 3 fix: recycler.sh PID-Sanity + Log-Akkurasie. Vorher: `pgrep -x claude` + `head -1` lieferte gelegentlich eine PID die zwischen pgrep und ps verschwand oder ein Zombie war → `ps -o rss= -p PID` returnte 0 → recycler loggte `recycled claude (rss_mb=0, ...)` und versuchte tmux respawn-pane, obwohl die PID gar nicht den eigentlichen claude-Prozess matchte. Sparky lief munter weiter, das Log war misleading. Fix in beiden `docker/mc-agent-base/recycler.sh` + `docker/mc-claude-agent/recycler.sh` (kein shared/recycler.sh — der Drift wird mit copy-paste-Pattern bestehen lassen, siehe Bug-Memo): (1) nach pgrep ein `kill -0 PID` Check als Existenz-Probe, (2) `RSS_KB=0` oder leer → skip mit WARN-Log statt fake-recycle, (3) `do_recycle` loggt "recycled claude" jetzt NACH erfolgreichem `tmux respawn-pane`, nicht davor — so spiegelt das Log die Realitaet. Greift bei naechstem Image-Rebuild. Kein Unit-Test (integration-heavy, identisch zu Bug 6 Begruendung).
- **2026-05-13** — Bug 4 fix: user-side `POST /api/v1/boards/{board_id}/tasks/{task_id}/comments` respektiert jetzt `comment_type`. Vorher: `CommentCreate` in `tasks.py:188` deklarierte das Feld nicht → Pydantic droppte es silent, DB-Default `message` griff. Der Operator sandte `{"comment_type":"feedback",...}` und bekam `comment_type:"message"` zurueck. Fix: `comment_type: str = "message"` + `field_validator` gegen `ALL_COMMENT_TYPES` (gleiche SoT wie agent-scoped POST). 4 neue Tests in `test_user_comment_type.py` (feedback durchgereicht, handoff durchgereicht, default message bei Omission, 422 bei unbekanntem type). Greift bei naechstem Backend-Rebuild.
- **2026-05-13** — Bug 6 fix: poll.sh false-positive Stagnation-Blocker. `STAGNATION_THRESHOLD` in `docker/shared/poll.sh` von 12 (60s) auf 36 (180s) angehoben + ENV-tunable (`STAGNATION_THRESHOLD` env-var). Vorher: lange LLM-Reasonings (Cogitated/Crunched Phasen ohne Tool-Output) loesten nach 60s einen automatischen `blocker`-Comment aus — Sparky bekam waehrend eines 12-Min-Cooks einen false-positive Blocker. Zusaetzlich: **final re-check** vor Blocker-Post — `sleep 2` + `detect_turn_state` + `turn_activity_hash` nochmal pruefen; bei working ODER Hash-Aenderung reset statt blocker. **Idempotency** via neuem `LAST_BLOCKED_TASK_ID` Marker: max 1 Blocker pro Task (reset wenn `run_task` einen neuen Task startet). Greift erst nach mc-agent-base + mc-claude-agent Image-Rebuild. Unit-Test entfaellt (integration-heavy tmux-Loop); Verifikation via Container-Smoke beim naechsten Rebuild.
- **2026-05-13** — Bug 2 fix: `agent_heartbeat` Self-Heal vs. drift zwischen `agents`-Row und `tasks`-Tabelle. Vorher: poll.sh sendet `status: idle` weil er keinen NEUEN Task hat → der Handler ueberschrieb agent.status pauschal auf "idle", auch wenn dem Agent ein `in_progress`-Task assigned war (live-Bug 2026-05-13: Sparky war 12 Min im Cook auf Voice-Foundation, in DB stand `status=idle, current_task_id=None, last_task_activity_at` eingefroren auf ACK-Zeit). Jetzt: Heartbeat liest die `tasks`-Tabelle. Wenn ein `in_progress`-Task an den Agent gepinned ist → `status="working"`, `run_state="running"`, `current_task_id=task.id`, `last_task_activity_at=now()` — egal was der Payload sagt. `blocked`/`review`/`done`/`failed` werden bewusst nicht als "aktiv" gewertet. Damit konvergiert der Agent-Row spaetestens beim naechsten Heartbeat (~30s) zur Wahrheit; Operator/Boss koennen keine zweite Task fahrlaessig zudispatchen weil current_task_id-Lock self-heals. 4 neue Tests in `test_heartbeat_status_sync.py` (active task ueberschreibt idle-Payload, blocked Task laesst idle durch, kein Task laesst idle durch, working-Payload ohne Task bleibt working) → 10/10 in `test_heartbeat_status_sync.py` + 3/3 in `test_heartbeat_context_pct.py` gruen, 65 Tests gesamt regression-clean. Backend-only fix.
- **2026-05-13** — Bug 10 fix: `paste_and_submit` in `docker/mc-agent-base/poll.sh` ist nicht mehr silent-fail. Neue Lib `docker/mc-agent-base/lib/paste-verify.sh` mit `verify_paste_landed FILE` — extrahiert die erste nicht-leere Zeile (auf `PASTE_FINGERPRINT_LEN` Zeichen gekuerzt, default 40) und prueft via `tmux capture-pane -S -100` ob diese Zeichenkette nach dem Paste im Pane sichtbar ist. `paste_and_submit` wickelt den paste-buffer+Enter-Block jetzt in eine Retry-Schleife (`PASTE_MAX_ATTEMPTS` default 2) mit Verify nach jedem Versuch. Bei finalem Fehlschlag: LOUD ERROR-Log + Return-Code 1 (Caller `run_task` kann es kuenftig propagieren). Default-Tunables: `PASTE_VERIFY_DELAY_SEC=2`, `PASTE_RETRY_DELAY_SEC=1`. Trigger: live-Bug 2026-05-13 — Re-Dispatch nach blocked->in_progress flip loggte `paste trotzdem (fail-open)`, Eingabe landete aber nicht im Pane, claude blieb idle. Tests: 7-case bash smoke + pytest wrapper (`backend/tests/test_paste_verify.{sh,py}`) — empty/blank-Files (optimistic 0), fingerprint match/miss, leading-blank-Zeilen, clipped 40-char Fingerprint, `PASTE_FINGERPRINT_LEN`-Override. Greift erst nach Container-Rebuild von `mc-agent-base` (poll.sh + neue lib werden via `COPY --chown=agent:agent lib /home/agent/lib` ins Image gebacken).
- **2026-05-13** — Bug 9 fix: Comment-Handoff zwischen Agents. `handoff` zu `DELIVERABLE_SYSTEM_TYPES` in `backend/app/comment_types.py` hinzugefuegt — Board Leads koennen jetzt via `mc comment handoff "<text>"` einen Worker auf einem existing assigned Task wachruetteln. `agent_comments.agent_add_comment` antwortet mit `delivery_hint` wenn ein default `message`-Comment auf einen fremden assigned Task gepostet wurde (kein Fail — nur Warnung; `mc` CLI rendert die Hint auf stderr). SOUL.md.j2 (orchestrator + is_board_lead) lehrt die Delegation/Briefing/Notiz-Unterscheidung mit Tabelle. mc CLI: `handoff` in `COMMENT_TYPES`, Help-Text annotiert delivered vs silent. Trigger: live-Bug Voice-Foundation 2026-05-13 — Boss postete `mc comment message "Briefing..."` an Sparky, Sparky pollte normal aber sah nichts. **Bewusst nicht runtime-spezifisch:** alle Runtimes nutzen `/me/poll` → der Fix wirkt fuer cli-bridge (Sparky), Host (Boss kuenftig), nicht-zementiert auf OpenClaw. Follow-Up Bug 10 (hermes-bridge.py liest `new_comments` aus `/me/poll` nicht aus — separat zu fixen wenn Host-Worker dazukommt). 4 neue Tests + 14 bestehende → 19/19 in `test_comment_delivery_via_poll.py`. Drift-Check in `test_comment_types_sot.py` (9/9) gruen.
- **2026-05-13** — ADR-033 (Proposed): Secrets vs Credentials Boundary kodifiziert. Keine DB- oder API-Aenderung. Klargestellt: `secrets` = System Token Wallet (1 pro Provider, Admin-only, keine agent-scoped Endpoints — fuer `openai_api_key`, `anthropic_api_key`, `github_token`, `openclaw_token`, `discord_bot_token`). `credentials` = Agent Task Vault (N pro Use-Case, typed login/token/custom, jeder User darf schreiben, Agents lesen via Scope `credentials:read`). Trigger: Voice-Foundation Inzident 2026-05-13 (Boss POST auf `/api/v1/secrets` → 401, weil System-Tokens admin-only sind und Voice-Foundation Secrets eigentlich Task-Credentials waren). Follow-Up-Commits: SOUL.md.j2 / TOOLS.md.j2 / dispatch.py-Templates / UI-Labels werden klargestellt — separat von diesem ADR. Siehe `docs/decisions/033-secrets-vs-credentials-boundary.md`.
- **2026-05-13** — Bug 5 permanent fix: `docker_agent_sync.py` re-rendert settings.json bei jedem Sync ueber `plugin_manager.sync_agent_plugins_to_disk()` aus dem `cli_agent_settings.json.j2` Template, statt nur das `model`-Feld zu mergen. systemPrompt-Drift zwischen DB-`agent.soul_md` und Disk-File ist damit geschlossen (war die Wurzel fuer Sparky+FreeCode Identity-Loss). Self-Check `len(soul_md) < 1000 → skip+warn` schuetzt vor Stub-State-Overwrites. Backward-kompatibel: `settings_path.exists()` bleibt Guard (Initial-Provisioning erstellt das File). 4 neue Tests + 23 bestehende → 27/27 gruen in `test_docker_agent_sync_runtime.py`. ADR-006 (Template→DB→File) wird explizit befolgt — kein neues ADR noetig.
- **2026-05-01** — Phase 26 Hermes Hardening vollständig (Plans 26-02..26-08, ADR-031). Drei strukturelle Fixes: (1) **Poll-Claim-Split** (HERM-10): `GET /agent/me/poll` setzt nicht mehr `status=in_progress + ack_at` atomar — Status bleibt `inbox` bis Agent explizit PATCH schickt, `dispatched_at < ack_at` Spanne garantiert (Migration 0018 ACK-Handshake wiederhergestellt). (2) **Per-Agent `idle_timeout_minutes`** (FND-06, Migration 0097): Deployer=30min, FreeCode/Davinci=20min, fallback chain via `dispatch_config` JSON-key — Watchdog killt keine langen Deploy-Tasks mehr mitten in der Arbeit. (3) **Deliverable Dual-Path** (HERM-14, HERM-11): Validator + FileResponse-Resolver akzeptieren Host-Form (`~/.mc/deliverables/{task_id}/`) UND Docker-Form (`/deliverables/{task_id}/`) — gleiche physische Datei via Volume-Mount; Path-Traversal-Schutz für beide Formen; `mc_register_deliverable` MCP-Tool liefert jetzt 201 via Admin-POST-Route (kein curl-Fallback mehr). Bonus: Bridge-Resilience (HERM-12): `KeepAlive: true` + crash-safe try/except + `SystemExit(1)` bei Crash → launchd startet Bridge innerhalb 5s neu (kill -9 verifiziert). Siehe ADR-031.
- **2026-05-01** — Phase 26 hardening Plan 26-02 (HERM-10 F1+F3): Poll-Claim split. `GET /agent/me/poll` setzt nicht mehr `status=in_progress` + `ack_at` atomar beim Inbox-Claim. Stattdessen: poll setzt nur `dispatched_at` (wenn None) + liefert Prompt + setzt `current_task_id`-Lock; Status bleibt `inbox`. Erst der Agent-eigene PATCH `status:in_progress` (tasks.py:1239-1241) flippt Status + setzt `started_at` + `ack_at`. Damit (a) sieht der UI-Status keinen "in_progress"-Sprung mehr bevor die LLM-Session den Prompt überhaupt gesehen hat (F1), (b) ist `dispatched_at < ack_at` mit messbarer Spanne garantiert (F3), (c) wird `started_at` deterministisch via PATCH-Pfad gesetzt (F2 als Side-Effect grün). Bridge bleibt timestamp-passive (poll.sh / hermes-bridge.py dedupten via `LAST_DISPATCHED_TASK_ID` / `_last_dispatched_task_id` cache → kein Re-Paste in tmux trotz wiederholtem `state=new_task` während pending ACK). Response-Payload erweitert um `task.status`, `task.dispatched_at`, `task.ack_at` für Beobachtbarkeit.
- **2026-04-30** — Phase 24: Hermes als 12. Agent integriert (host-side tmux Worker, single-instance Runtime, eigene `scripts/hermes-bridge.py`). Neuer DB-Feld `runtimes.single_instance: bool` als generisches Non-Switchable-Pattern; vLLM-Provider Reuse mit Sparky (Qwen/Qwen3.6-35B-A3B-FP8 @ 192.0.2.10:8000); KEIN Eintrag in `docker/docker-compose.agents.yml` (host-side, generator-managed). Siehe ADR-029.
- **2026-04-29** — Phase 16 Runtime Registry Konsolidierung + Session-Env-Propagation (ADR-028, erweitert ADR-027). **DB-only Registry:** `GET /runtimes` + `GET /runtimes/{id}` lesen via neuem `runtime_manager.list_db_runtimes(session)` aus der `runtimes`-Tabelle; `load_registry()` (JSON) bleibt nur noch als Lifespan-Bootstrap-Seed. Migration 0094 idempotent (INSERT-only für fehlende Slugs, niemals UPDATE/DROP). **`build_runtime_env(rt)` Helper** in `runtime_manager.py` extrahiert + 5 Unit-Tests — claude-Image → `ANTHROPIC_AUTH_TOKEN`, openclaude-Image → `OPENAI_API_KEY` + `OPENAI_BASE_URL`. **Same-Image Switch via `respawn_window_only`:** `restart_docker_agent_container(respawn_window_only=True)` ruft `_respawn_agent_window` (`docker exec mc-agent-{slug} tmux respawn-window -k -t {slug}:0`) — poll.sh + Recycler überleben, <5s. `wait_for_agent_healthy(respawn_mode=True)` pollt `tmux capture-pane` mit Ready-Signalen (`╭─` / `❯` / `> ` / `$ `) und dismissed Modell-Picker einmalig per Enter. `agent_runtime_switch.switch_agent_runtime` dispatcht `respawn_window_only` (same-image) vs `force_recreate` (cross-image) per `detect_image_change()`. **POST `/api/v1/runtimes/{id}/probe-model`** (re-uses Phase-15 `probe_runtime_model`, persistiert Ergebnis, 422 bei `cloud`-Type, `data[0].id` als kanonisch). **Frontend Cache-Coherence:** `staleTime: 0` für `runtime-switch-preview`, `["runtimes"]` + `["agents"]` + `["agent", id]` + `["runtime-switch-preview", id]` Invalidate nach Mutationen. Re-probe-Button im `/runtimes`-Card-Footer (vLLM-Reload-Use-Case). **Docker-Compose-Plumbing für Cross-Image-Switch:** Backend-Image bekommt `docker-compose-plugin` zusätzlich zu `docker-ce-cli`; `docker-compose.yml`, `docker/`, `.env` werden unter dem absoluten Host-Pfad ins Backend gemountet; `force_recreate`-Subprocess setzt `HOME=$HOME_HOST` damit `${HOME}`-Substitutionen in den Compose-Files den Host-Pfad treffen statt `/home/mcuser`. **Live-Verify (D-13, 2026-04-29):** Cross-CLI-Switch an Tester (claude → openclaude) + Same-Image-Switch an Sparky (vLLM Qwen → Ollama Cloud) durchgespielt. Zusätzlich: Pitfall-3 aus RESEARCH.md korrigiert — tmux-Session-Name = `slug` (lowercase, aus `AGENT_NAME` env in `docker-compose.agents.yml`), nicht `agent.name`. **Test-Delta:** +9 backend (`test_runtimes_db_list`, `test_build_runtime_env`, `test_runtimes_probe`) + erweitert (`test_docker_agent_sync_runtime`, `test_agent_runtime_switch`). **D-22 deferred** (kein periodisches Background-Probing — Re-probe-Button reicht). **Rollback:** `respawn_window_only`-Branch in `restart_docker_agent_container` entfernen + Switch-Service immer `force_recreate=image_change` aufrufen wie pre-Phase-16; DB-Reads in GET-Handlern auf `load_registry()` zurückfallen lassen.
- **2026-04-28** — Phase 15 Universal Agent ↔ Runtime Binding (ADR-027). NEUER Service `backend/app/services/agent_runtime_switch.py` orchestriert atomare Runtime-Wechsel mit DB→Files→Compose-Render→Container-Restart→Health-Check + vollem Rollback bei jedem Fehler. NEUER Service `backend/app/services/compose_renderer.py` rendert `docker/docker-compose.agents.yml` aus dem DB-State (DB ist Single Source of Truth für Image-Tags pro Agent — Cross-Image Switches `cloud ↔ vllm/lmstudio` funktionieren jetzt; vorher silent broken). Redis-Lock `mc:agent:{id}:runtime-switch` (TTL 120s) gegen Concurrency. 6 typed Exceptions (`RuntimeNotFoundError 404 / RuntimeIncompatibleError 422 / AgentNotSwitchableError 422 / AgentBusyError 409 / RuntimeSwitchLockTimeout 409 / SwitchHealthCheckFailed 503`). PATCH `/agents/{id}` delegiert an Switch-Service. NEUER Endpoint `POST /agents/{id}/preview-runtime-switch` (dry-run). NEUER SSE-Endpoint `GET /agents/{id}/terminal-events/stream` und Redis-Channel `mc:agent:{id}:terminal:remount` damit die Sessions-Seite den WebSocket nach externem Switch automatisch re-mountet. Frontend: `RuntimeSwitchModal` (dry-run preview + Image-Banner + Compat-Warnings + Force-Toggle bei in-progress) ersetzt `window.confirm`; `BindAgentModal` + Bound-Agents Footer auf `/runtimes` RuntimeCards; `RuntimePill` extrahiert nach `components/shared/` (default + compact); `useTerminalRemountSignal` Hook. `agent.runtime_switched` und `agent.runtime_switch_failed` Activity-Events. Test-Delta: +14 backend (`tests/test_agent_runtime_switch.py`), +1 backend (`tests/test_agent_runtime_patch.py` erweitert), +8 frontend vitest (`RuntimeSwitchModal`, `RuntimePill`, `useTerminalRemountSignal`). Backend-Suite **1414 passed / 1 skipped** nach Wave 2. ADR-018 als "Erweitert durch ADR-027" markiert. **Rollback:** `agent_runtime_switch.py` löschen + PATCH-Pfad in `agents.py` zurück auf inline DB+sync+restart wie pre-Phase-15. compose_renderer kann standalone bleiben (idempotent, nur dann aktiv wenn Switch-Service ihn aufruft).
- **2026-04-27** — Phase 7 Obsidian View-Only Export (OBS-01..04, v0.5). **Kein neues ADR (D-28-analog):** Phase 7 ist ein unidirektionaler Read-Only-Mirror auf bestehende Boundaries (BoardMemory + MSY-03 Attachments) — keine neue strukturelle Entscheidung. **OBS-01 Vault-Layout (Plan 07-01):** NEUE Service-Datei `backend/app/services/obsidian_export.py` (698 Zeilen) — `ObsidianExportService` Singleton (mirror von `EmbeddingRetryLoop` Plan 05-02 Pattern, jetzt 4× im Code). 5 Vault-Subdirs auf `${HOME_HOST}/.mc/vault/{memory/{agents,projects,global},attachments/{tasks,deliverables}}/` werden beim ersten `.start()` idempotent angelegt. `_vault_root()` Resolver-Kette `HOME_HOST` → `HOME` → `expanduser('~')` (gleicher Pattern wie `_attachments_root()` Plan 05-06 — `feedback_home_host_pattern.md` strikt eingehalten, niemals `expanduser` standalone). `_safe_join()` Path-Traversal-Guard via `os.path.realpath` + `startswith` raises `RuntimeError` bei Escape (Service-Context — nicht `HTTPException` wie in Routern). 4 NEUE Settings: `obsidian_export_interval: int = 300` + `obsidian_export_enabled: bool = True` (Kill-Switch). NEUER `RedisKeys.obsidian_export_lock()` → `mc:obsidian_export:lock` (cross-Worker-Dedup, `ex=interval` TTL). **OBS-02 Export-Pipeline (Plan 07-02):** 5 Helper-Funktionen + `trigger_cycle()` Body. `_render_frontmatter(entry, agent_slug, project_slug)` rendert deterministische 7-Key YAML in literal insertion order (`title, type, tags, date, agent, project, status` — Pitfall 1 closure: `yaml.safe_dump(sort_keys=False)` + Python-3.7+-dict-order Garantie). `_render_body(entry)` rendert `# {title}\n\n{content}\n\n---\n<footer>` mit IMMER vorhandenem Footer (auch bei leerem source/linked — deterministische Output-Shape required für SHA-256-Idempotency). `_atomic_write(target, content)` via `tempfile.mkstemp(dir=parent)` + `os.replace` (POSIX-Atomic-Garantie, kein Partial-Write-Risiko). `_write_if_changed(target, content)` → False bei SHA-256-Identität (mtime preserved — Idempotency-Invariante). `_resolve_agent_slug` + `_resolve_project_slug` (Routing via `agent.name` bzw. `board.default_project_id` → `project.name`; Fallback `_unprojected/{board-short}/`). Lifespan-Registrierung in `main.py:127/156` (start nach `embedding_retry.start()`, stop in matching reverse order). Per-Row Exception-Isolation in `trigger_cycle()` mirrors `intelligence.py:_analyze` Pattern — eine korrupte BoardMemory-Zeile abort den Cycle nicht. Live Smoke gegen Dev-PostgreSQL: 457 Rows → 457 .md Files in einem Cycle, zweiter Cycle 0 writes / 457 skipped (Idempotency live verifiziert). **OBS-03 Attachment-Mirror + Wiki-Links (Plan 07-03):** 3 NEUE Helper. `_resolve_collision_safe_attachments(attachments)` weist `display_name` mit sha16-Prefix-Segment zu wenn `original_name` 2+ mal innerhalb derselben memory_id auftritt (Pitfall 5 closure: zwei gleichnamige Attachments unter derselben Entry resolven zu distinkten Bildern). `_mirror_attachment(src, dst)` via `shutil.copy2` (preserved mtime → idempotenter zweit-Cycle-skip on size+mtime match). Defensive: missing source → WARN log + return False statt raise (mirror per-row Pattern). `_rewrite_wikilinks(body, attachments, memory_id)` mit BOUNDED regex `r"!\[[^\]]*\]\(" + re.escape(needle) + r"\)"` per known attachment URL → `![[display_name]]` Obsidian Wiki-Link (Pitfall 6 closure: nur EXAKTE bekannte Attachment-URLs werden umgeschrieben — User-authored unrelated `![cat](https://example.com/cat.jpg)` bleibt verbatim). T-7-03-01 Source-Path-Traversal-Guard (`os.path.realpath(src).startswith(_attachments_root + os.sep)`) in `trigger_cycle` BEVOR `_mirror_attachment` aufgerufen wird. T-7-03-02 Destination-Side via `_safe_join` (Plan 07-01). Konsumiert MSY-03 Storage-Layout `${HOME_HOST}/.mc/attachments/{board|_global}/{memory_id}/{sha16}-{original_name}` direkt. Per-Attachment Exception-Isolation. **OBS-04 MCP-Passthrough Spike (Plan 07-04):** `.planning/spikes/obsidian-mcp-passthrough.md` (121 Zeilen). **Verdict: INVALIDATED** für Headline-Frage "Kann MC MCP-Traffic durch OpenClaw-Gateway passen?" basiert auf 791-line Audit von `openclaw_rpc.py` (27 `request("...")` Call-Sites enumeriert, ZERO `mcp.*` Methoden — `grep -E 'request\("mcp' = 0`). PARTIAL-Nuance dokumentiert als Finding 4: Gateway transportiert MCP-Server-*Liste* via `config.patch` + `sessions_reset`, aber MCP-Traffic selbst (Tool-Call-Request/Response) crosst Gateway nie. **Recommendation:** v0.6+ Obsidian-MCP-Integration als standalone stdio-Server in `~/.openclaw/mcp-servers/obsidian/` via existing MCP Registry (ADR-016) + per-Agent Allowlist — keine Gateway-Changes, keine Schema-Changes, keine Phase-7-Follow-up-Plan. **Verkettung mit Phase 5:** OBS-03 Attachment-Mirror konsumiert Plan 05-06 (MSY-03) Storage-Layout 1:1 — Phase 7 fügte keine neuen Schema-Changes hinzu, nur einen Read-Sink. **Test-Count-Delta:** Phase 6 Close 1348 passed → Plan 07-00 Wave-0 1348 + 11 xfailed (4 OBS-Stub-Files + 1 Spike-Gate) → Phase 7 Close **1366 passed / 1 skipped / 0 xfailed / 0 failed** (+18 net über Plans 07-01..04; jeder Wave-0-Stub geflippt; +5 Bonus-Tests gegen Plan-Soll). Frontend-v2 vitest unverändert **14 passed / 0 failed** (Phase 7 ist backend-only — kein Frontend-Touch). Phase 1 Race-Tests `test_dispatch_race.py` **3/3 grün** durchgehend (REF-03 Contract gehalten). **Module-Diffs (gemessen via git diff --stat):** obsidian_export.py NEW +698 + main.py +8 (lifespan import + start + stop) + config.py +12 (2 Settings) + redis_client.py +5 (1 RedisKeys helper) + 5 NEUE Test-Dateien + 1 NEUE Spike-Datei = ~+730 insertions across 9 production files (ohne Tests). **Manual Smoke (Plan 07-05 Task 3 — DEFERRED to Operator via checkpoint:human-verify):** Der Operator öffnet `~/.mc/vault/` in Obsidian, prüft Frontmatter rendert als Properties UI + Wiki-Link `![[name]]` rendert inline Image-Preview. **Rollback:** `OBSIDIAN_EXPORT_ENABLED=false` in `.env` + `docker compose restart backend` → Loop überspringt Cycles silently, keine FS-Writes. View-only by design — keine Reverse-Sync, keine BoardMemory-Mutationen, MC bleibt Single Source of Truth.
- **2026-04-27** — Phase 6 Context Management & Auto-Recovery (CTX-01..03 + REC-01..03, v0.5). **ADR-026 Accepted.** **CTX-01 (Plan 06-02 + 06-03):** Docker claude-binary Agents self-reporten Context-Window-Usage via `poll.sh` tmux-statusline-Scrape (zwei-Strategie: `tmux display-message -p '#{pane_title}'` primär, `tmux capture-pane | tail -10 | grep -oE 'ctx[: ]+[0-9]+%?'` Fallback, Shell-Injection-Mitigation via env-var Passing in `python3 -c` + bash-Regex-Sanitize `^[0-9]+$` und `>100` Guard). Backend `AgentHeartbeatPayload` extended mit `context_pct: float | None = Field(default=None, ge=0, le=100)` (Pydantic 422 auf garbage). Handler write path inverts display-Formel: `agent.context_tokens = round(payload.context_pct / 100 * agent.context_max)`. `/internal/bootstrap` exposed `tokens["CONTEXT_MAX"] = str(agent.context_max or 200_000)` als Fallback-Denominator + Container-Side `entrypoint.sh` exportiert es mit `${CONTEXT_MAX:-200000}` Chain (bootstrap > existing env > 200000 default). Backward-compatible: scrape-failure omittet context_pct, backend preserved prior `context_tokens`. **CTX-02 (Plan 06-04):** `_compact_overflowed_sessions` (in `services/watchdog/session_monitor.py`, +167 lines, 1 neue Methode) ersetzt buggy `_reset_overflowed_sessions`. Threshold 80% → 85% (`COMPACTION_THRESHOLD = 0.85` Klassen-Konstante). Flow: `agent.compaction` event → checkpoint-Instruktion via `rpc.chat_send` (Deutsch, Format: `Task / Status / Naechste Schritte / Offene Fragen`, max 500 Wörter) → `asyncio.sleep(60)` → session reset via `runtime_context.get_session_context_for_runtime` (CLAUDE.md "Absolute Verbote" enforcement: niemals direkter `rpc.sessions_reset()` Call). Per-(agent) Redis-Dedup-Lock `RedisKeys.compaction_lock(agent_id)` mit `ex=90` (covers 60s Wait + Margin). Kill-Switch: `settings.context_compaction_enabled: bool = True` (default-on, ADR-026 Rollback). Deprecated `_reset_overflowed_sessions` body bleibt im Code mit `# DEPRECATED Plan 06-04` Marker als Kill-Switch-Fallback (lint-grepable). Watchdog tick loop call site (`watchdog/core.py:141`) flipped auf neue Methode. **CTX-03 (Plan 06-04 + 06-06):** Neuer `agent.compaction` Event mit strukturiertem detail-Dict (`context_pct`, `total_tokens`, `context_limit`, `checkpoint_summary_received`, `task_active`); severity=info. `total_compactions` counter auf Agent-Modell wird inkrementiert. Frontend `ActivityFeed.tsx` `eventTypeToStatus` Map extended um 4 Phase-6 Einträge — keine JSX-Änderungen, keine StatusType-Union-Erweiterung. **REC-01..03 (Plan 06-01 + 06-05):** `_run_tiered_recovery` (in `services/task_runner.py`, +176 lines, 1 neue Methode) ersetzt direkte Stale-Eskalation in `_check_stale_in_progress`. Tier 1: heartbeat probe `asyncio.timeout(10)` — short-circuit bei OK (Agent alive but quiet); Tier 2: per-Runtime restart (docker → `asyncio.to_thread(restart_docker_agent_container, agent)`; host → `await _host_agent_lifecycle(agent, "restart")`; cli-bridge / openclaw → skip mit debug-log) + 30s Wait nach Restart; Tier 3: task resume mit Structured Recovery Recap composed aus `_ctx.recovery_recap` (Absolute Verbote Intro) + `build_recovery_context` extras (checklist + last 5 lifecycle comments) via `rpc.chat_send(..., reset_session=True)`; Tier 4: `emit_event(severity='error')` für Auto-Discord-Fan-out via existing `activity.py:73-80` Webhook-Pfad — der Operator wird nur gepaged wenn ALLE drei Auto-Tiers fehlschlagen. Per-(agent, task) Redis-Dedup-Lock `RedisKeys.recovery_inprogress(agent_id, task_id)` mit `ex=600` (10min TTL covers Tier 1+2+3 Budget). Task-Status bleibt `in_progress` durch alle Tiers — nur Tier 4 ändert effektiv etwas am Lifecycle. **REC-03 als Audit-Log:** Activity Events serve as Audit-Trail (D-23/24 — kein neues DB-Table). 4 neue event_types: `agent.compaction`, `agent.recovery_started`, `agent.recovery_tier_complete` (3× — once per Tier 1/2/3), `agent.recovery_failed`. severity='error' triggert Discord-Webhook automatisch via existing infrastructure. **Neue Redis-Namespaces (Plan 06-01):** `RedisKeys.compaction_lock(agent_id)` → `mc:compaction:{agent_id}` + `RedisKeys.recovery_inprogress(agent_id, task_id)` → `mc:recovery:inprogress:{agent_id}:{task_id}`. Kollisionsfrei — `grep -r "mc:compaction\|mc:recovery:inprogress" backend/` returned zero Pre-Existing-Matches. **Test-Count-Delta:** Backend Plan 05-07 Baseline 1330 passed → Phase 6 Close **1348 passed / 1 skipped / 0 xfailed / 0 failed** (+18 net; jeder Wave-0-Stub geflippt durch named follow-up Plan + Plan 06-02 lieferte 2 Bootstrap-Extras + Plan 06-04 lieferte 2 Compaction-Extras — counter increment + kill-switch Pfad). Frontend-v2 vitest 10 → **14 passed / 0 failed / 5 files** (+4 / +1 file via Plan 06-06). Phase 1 Race-Tests `test_dispatch_race.py` **3/3 grün** durchgehend (REF-03 Contract gehalten). **Module-Diffs (gemessen via `git diff --stat`):** task_runner.py +216/-33 + session_monitor.py +167 + redis_client.py +14 + agents.py +10 + internal.py +5 + config.py +7 + ActivityFeed.tsx +7 = **+393 insertions / -33 deletions across 7 files** (Phase-6 Implementation, ohne Tests). **Live Chaos-Smoke:** Plan 06-07 Task 3 Schritt 6 — kill claude-PID inside `mc-agent-tester` Container while task in_progress → MC restartet Container + resumed Task via Tier-3-Recap → no Tier-4-Page. Verification deferred to the operator's manual smoke (autonomous: false sign-off, `checkpoint:human-verify`). **Rollback:** `settings.context_compaction_enabled = False` + `docker compose restart backend` → CTX-02 fallback auf Bug-kompatibles `_reset_overflowed_sessions`. REC kein Kill-Switch (Stale-Pfad bleibt rückwärtskompatibel zur Approval-Eskalation als Tier-4-Fallback). **Phase 7 (Obsidian View-Only Export) UNBLOCKED** — Phase 6 hat keine MSY-/MEM-Dependencies eingeführt, OBS-* kann nun starten.
- **2026-04-27** — Phase 5 (MSY-01..05) Memory System Hardening. **Kein neues ADR (D-28):** Phase 5 verfeinert bestehende Boundaries (BoardMemory-Schreibpfad + /memory-API + dispatch-Resilienz) ohne neue strukturelle Entscheidung. **MSY-01 Reflection-Fold:** `record_task_completion` (in `services/auto_memory.py`) liest jetzt `comment_type='reflection'`-Comments und schreibt pro Reflektion eine `BoardMemory(memory_type='journal', tags=['auto','reflection_fold',...])`-Zeile. Per-Reflection-Dedup via `mc:auto_memory:reflection_fold:{task_id}:{sha256(text)[:16]}` Redis-Key (30-Tage-TTL). Lazy-Backfill für Legacy-Reflections (Fold läuft AUSSERHALB des `auto_memory_task_done`-Short-Circuits). Bestehende `agent_comments.py:395-422` Reflection→Lesson-Pipeline bleibt byte-identisch (Pitfall 1 — zwei Schreibpfade by design: `lesson` agent-scoped bei Comment-Post + `journal` board-scoped bei Task-Completion). **MSY-02 Dedup + MERGE-Badge:** Hash-Dedup mit `_normalize_content_for_hash` + `_content_hash` in `routers/memory.py` (gleiche Formel wie Migration 0091 Backfill — Single Source of Truth). Cosine-Flag via NEUEM `services/memory_indexing._find_merge_candidate(layer, vector, board_id, agent_id, threshold)` mit Qdrant top_k=1, setzt `merge_candidate_id` bei score ≥ `settings.memory_merge_threshold` (default 0.9). 3 NEUE Endpoints (`POST /knowledge/{id}/merge_into/{target_id}` + `keep_both` + `unrelated`). 2 NEUE Frontend-Komponenten (`MergeCandidateBadge.tsx` violet pill + `MergeResolutionPanel.tsx` inline expansion mit 3 Action-Buttons), gewired in 3 Card-Grids + MemoryModal. **MSY-03 Attachments:** 3 NEUE Endpoints (`POST/GET/DELETE /knowledge/{id}/attachments[/{filename}]`) mit MIME-Allowlist (5 types) + 10 MB Size-Cap + 5-Files-pro-Entry-Cap + 3-Schicht Path-Traversal-Guard (literal `..`/`/`/`\\` Check auf raw filename VOR `os.path.basename`, dann `os.path.realpath` + `startswith` Check). HOME_HOST-Resolver-Chain in `_attachments_root()` (`HOME_HOST` → `HOME` → `expanduser('~')` — niemals `expanduser` standalone, per `feedback_home_host_pattern.md`). Cascade `shutil.rmtree` bei `DELETE /knowledge/{id}`. SVG bewusst nicht erlaubt (XSS-Risiko via embedded `<script>`). 3 NEUE Frontend-Komponenten (`AttachmentPanel` + `AttachmentThumb` 120×80 Bild / 80×80 PDF-Icon-Card + `AttachmentLightbox` Radix-Dialog Full-Screen-Viewer). **v0.5 Scope-Decision (W7):** In-App-Upload-UI deferred — `AttachmentPanel editMode={false}` hardcoded in `/memory/page.tsx`; Backend-Endpoints voll funktional + getestet. **MSY-04 Embedding-Resilience:** NEUER Singleton `services/embedding_retry.py` (293 Zeilen, mirror von `intelligence.py` — Pattern jetzt 4× im Code). `EmbeddingRetryLoop` mit `start/stop/_run_loop/_drain_once/_process_one` + module-level `enqueue()` + `get_dropped_total()`. `RETRY_BACKOFFS_SEC = (60, 300, 900, 3600, 21600, 21600, 21600, 21600)` — 8 Versuche über ~24h. `MAX_QUEUE_LEN = 1000`, `DRAIN_BATCH_SIZE = 50`. `embedding_service.health_check` umbenannt → `is_available()` mit `asyncio.wait_for(timeout=2.0)`. `memory_indexing.index_memory` except-Branch wired mit `_enqueue_embedding_retry` — fail-soft to fail-soft (BoardMemory landet trotzdem; Retry-Tracking ist best-effort). Lifespan-Registrierung in `main.py` (start nach `intelligence.start()`, stop vor `runtime_schedule_service.stop()`). `Settings.embedding_retry_interval = 60` default. **Roadmap Success Criterion 4 empirisch bewiesen:** `test_dispatch_unaffected_by_outage` läuft `asyncio.wait_for(index_memory, timeout=1.0)` und schliesst in 0.49s ab wenn `embed()` ConnectionError raisst — Dispatch ist nie länger als 1s blockiert. **MSY-05 Scope-Filter:** Backend `GET /api/v1/knowledge?scope=global|board|agent|all` Query-Param via FastAPI `Literal[...] | None`. Frontend `MemoryPage.tsx` 3-branch `as const` Ternary ersetzt silent `{}`-Fallback. `api.knowledge.list` + `listByLayer` extended mit `scope?:` Type. **Migration 0091 (additiv, alle Spalten nullable):** `board_memory.content_hash TEXT NULL` indexiert + `merge_candidate_id UUID NULL FK self-ref ON DELETE SET NULL` indexiert + `attachments JSON NULL`. Python-loop content_hash-Backfill (pgcrypto-Extension-frei, gleiche Formel wie Plan 05-05 Runtime-Helper). Phase 3 Plan 03-03 Lesson honouriert: KEIN `server_default` — null = "not set" verschieden von explizitem Wert. Pitfall 5: `attachments` nutzt `default=None` (NICHT `default_factory=list` — SQLAlchemy mutable-default-arg-Trap). Frontend `BoardMemoryAttachment` Interface + 3 optional `?`-Fields auf `BoardMemory` Interface (backward-compatible für die 27+ `lib/types.ts`-Dependants). **Behaviour-preserving:** Alle neuen Spalten nullable + kein DB-DEFAULT → kein Datenrisiko bei Rollback; Pitfall 5 strikt eingehalten. **Neue Dateien:** `backend/app/services/embedding_retry.py`, `backend/alembic/versions/0091_memory_dedup_attachments.py`, 5 Frontend-Komponenten (`MergeCandidateBadge.tsx`, `MergeResolutionPanel.tsx`, `AttachmentPanel.tsx`, `AttachmentThumb.tsx`, `AttachmentLightbox.tsx`), 6 Test-Dateien (`test_auto_memory_reflections.py`, `test_memory_dedup.py`, `test_memory_attachments.py`, `test_embedding_retry_queue.py`, `test_knowledge_scope_filter.py`, `test_migration_0091.py`). **Test-Count-Delta:** Phase 4 Baseline 1310 passed → Plan 05-00 Baseline 1310 + 20 xfailed → Phase 5 Close **1330 passed / 1 skipped / 0 xfailed / 0 failed** (+22 net relative zur Roadmap-Schätzung; +20 Tests landed über Plans 05-01..06; jeder Wave-0-Stub geflippt). Frontend-v2 vitest **10 passed / 0 failed**. Phase 1 Race-Tests 3/3 grün durchgehend (REF-03 Contract gehalten). **Phase 6 (Context Management & Auto-Recovery) + Phase 7 (Obsidian View-Only Export) UNBLOCKED** — Phase 7 OBS-03 Attachment-Mirroring konsumiert Plan 05-06's `{HOME_HOST}/.mc/attachments/{board|_global}/{memory_id}/sha-prefix-name` Directory-Layout direkt.
- **2026-04-26** — Phase 3 (MEM-01) Memory Leak Root-Cause Fix. **Recycler-System (ADR-024):** Bash watchdog `recycler.sh` als tmux Window 2 in beiden Docker-Agent-Image-Targets (mc-agent-base + mc-claude-agent). Pollt alle 60s, recyclt `claude` bei idle ≥15min ODER RSS >1500MB via `tmux respawn-pane -t <session>:0 -k` (Container bleibt up, claude-PID wechselt sauber). Two-tier kill-switch: env-var `AGENT_RECYCLER_ENABLED` (global default-on, `${AGENT_RECYCLER_ENABLED:-true}` in compose) + `agents.recycler_enabled BOOL NULL` per-agent override (Migration 0090). **Tmux-Layout (Docker-Agents) erweitert:** Window 0 (claude) + Window 1 (poll.sh) + **Window 2 (recycler.sh) NEU**. PID-1 watchdog hat dritte case-block für Window 2. Backend env-render in `docker_agent_sync` schreibt `AGENT_RECYCLER_ENABLED` in `claude-config/.env` bei sync-config; `/internal/bootstrap` liefert den Key fürs Live-Refetch. **Sparky out of scope by design:** `pgrep -x claude` (exact match) trifft auf Sparkys `openclaude`-Binary nicht → silent no-op ohne Code-Path-Branch. **Soak-Validation:** 7-Tage-Soak-Window läuft seit 2026-04-26T11:50:04Z. Sign-off in Plan 03-07 (T+7d ≥ 2026-05-03) befüllt ADR-024 mit before/after numbers. **Rollback:** `AGENT_RECYCLER_ENABLED=false` in `docker/.env.agents` + `docker compose ... up -d --force-recreate` → recycler.sh self-disabled via `exec sleep infinity`; claude akkumuliert wieder wie vor Phase 3.
- **2026-04-20** — Review-Policy Trust-by-Default (ADR-023). `boards.require_review_before_done` auf `mc-dev` von `True` auf `False` geflippt (Migration 0088). Reflexion entkoppelt von Board-Flag: Guard in `agent_scoped.py` checkt jetzt `enforce_reflection` + Closing-Transition (review/done), unabhaengig vom Board. `SOUL.md.j2` hat neuen `Review-Policy`-Block im `role != "orchestrator"` shared Bereich der klar sagt wann `mc review` Pflicht ist und wann direkt `mc done` OK ist. Guard-Check prueft jetzt "existiert eine Reflection" statt "ist letzter Kommentar" — Progress-Updates nach Reflexion sind jetzt unproblematisch. 1020 Tests gruen (test_predone_validation, test_task_events, test_workflow_scenarios mit expliziten Reflection-Posts ergaenzt).
- **2026-04-21** — `~/.mc/` Home + Workspace-Layout-Standardisierung. Migration 0087 setzt `agents.workspace_path` konsistent auf `~/.mc/workspaces/<slug>`. Docker-Mount-Konvention: `/workspace` (rw, per-Agent) + `/workspace-ref` (ro, die `~/Workspace/Projects/` des Operators — nur Rex/FreeCode/Sparky/Tester/Deployer/Researcher) + `/deliverables`. Shakespeare + Davinci bekommen KEIN `/workspace-ref` (Security-Reduktion). Backend bekommt `HOME/.mc` zusätzlich gemountet (via docker-compose.yml). `_container_workspace_path()` Helper in `dispatch.py` übersetzt Host-Pfade zu Container-Pfaden im Prompt. Symlink-basierte Backward-Compat: `~/.openclaw/{agents,mcp-servers,plugins,skills} → ~/.mc/<same>`. Alte Workspaces archiviert als `~/.openclaw/*.pre-mc-migration/`. ADR-022.
- **2026-04-20** — Harness Phase 2 + Agent Personas. `mc` CLI (`scripts/mc-cli/`) ersetzt ~2000 Zeichen curl-Boilerplate in der Dispatch-Message durch Commands (`mc ack / done / review / blocked / failed / comment / checklist / question / help / deliverable / memory search`). Dispatch-Budget 2000 / 2500 / 4000 Zeichen mit `_assemble_with_budget` helper. Memory on-demand via neuem `GET /me/memory/search`. Progress konsolidiert auf TaskChecklistItem (Migration 0082 migriert checkpoint-Comments zu progress; `POST /checkpoint` → 410 Gone). Per-Agent Persona in DB (`agents.soul_persona_md`, Migration 0084 + Seed 0085). Team Reflection Charter + REFLECTION_REQUIRED_FIELDS in `backend/app/constants.py` als Single Source of Truth. Henry auf Messenger-Scope reduziert (Migration 0083). Neo + Planner entfernt (Migration 0086). Workspace-Konvention: `.mc-scratch/` (gitignored) + `.mc-deliverables/{task_id}/` (committed). ADR-020 + ADR-021.
- **2026-04-19** — LLM Runtime Registry in DB + per-Agent Runtime-Switching für cli-bridge Docker-Agents. Migration 0077 (runtimes table + agents.runtime_id FK), Migration 0078 (qwen-coder-lms seed + Sparky-Link). `docker_agent_sync` + `/internal/bootstrap` injizieren OPENAI_BASE_URL + OPENAI_MODEL aus der DB; entrypoint.sh exportiert beide. Docker-compose Sparky-Hardcode entfernt. Neue runtime_types: `unsloth` (tmux-lifecycle) + `openai_compatible` / `cloud` (probe-only). Frontend: Runtime-Dropdown in Agent-Detail mit farbigem Left-Border je Runtime-Typ, Locked-Badge für Boss/Henry. ADR-017 + ADR-018.
- **2026-04-18** — Boss Install System Phase 2 (MCP): Neues Feld `agents.mcp_servers` + `agent_templates.mcp_servers` (JSONB Allowlist). Neue Services: MCPRegistry (`mcp_registry.py`) + MCPSync (`mcp_sync.py`). Neue Approval-Types: `install_mcp` + `uninstall_mcp`. Install-Executor: `_install_mcp` / `_uninstall_mcp` Handler mit Smoke-Test-Rollback. Admin-Endpoints: `/api/v1/mcp-servers/*` + `PATCH /agents/{id}/mcp-servers`. Frontend: Settings → MCP Section + Agent-Detail → MCP Tab mit MCPServerMatrix. **Pending**: Docker-Mount `~/.openclaw/mcp-servers:/mc-servers:ro` (benötigt Agent-Restart). Siehe ADR-016.
- **2026-04-18** — Boss Install System Phase 1: Neuer Endpoint `POST /api/v1/agent/install-requests` (agent-scoped, 5 Guards). Neue Approval-Types: install_skill, uninstall_skill, install_plugin, uninstall_plugin. Neuer Service: InstallExecutor (Service-Layer-direct). Neues Table: install_log (Audit-Trail + Rollback-Pointer). Frontend: InstallRequestCard im Inbox. Phase 2 (MCP) + Phase 3 (Gateway) als separate Releases. Siehe ADR-015.
- **2026-04-18** — Dispatch-Fix für `host` Runtime: `NON_GATEWAY_RUNTIMES` konstante in `dispatch.py` (zentrale Quelle). `find_dispatch_target()` erkennt `host` jetzt als "always online", `auto_dispatch_task()` akzeptiert pre-assigned Host-Agents, `_extract_auth_token()` liefert `$MC_AGENT_TOKEN` auch für Host. Dedizierter `host_poll` Dispatch-Branch (kein RPC, Task bleibt inbox → poll.sh claimt)
- **2026-04-17** — Boss-Host-Migration: Boss läuft als macOS launchd-Job (`com.openclaw.boss`) mit echtem `claude`-Binary (Opus 4.7, OAuth) statt im Container. Neuer Runtime-Typ `host` (Migration 0073). ttyd + WS-Proxy für Browser-Terminal. Container-Boss in `docker/docker-compose.agents.yml` auskommentiert. Authoritative Scripts in `docker/boss-host/`. ADR-014
- **2026-04-12** — Phase F (Memory 3-Layer-Rewrite), Phase G (ReflectionForm), Phase H (Plugin-Audit-Tab), Phase I (Consensus-Helper). ARCHITECTURE.md komplett aktualisiert
- **2026-04-11/12** — Boss-Autonomy + 3-stufiges Memory-System (Qdrant). 6 Phasen + Follow-Ups A-E, G. Planner entfernt (Migration 0071). enforce_reflection=True. 68+ Memories in Qdrant indexiert. 819 Backend-Tests gruen
- **2026-04-08** — MC V2 Docker-Agents live deployed. 8 Deployment-Bugs gefixt (Docker socket permissions, Network-Isolation, tmux nologin Zombie, PID 1 CPU-Spin, Keystroke-Forwarding, Endpoint-Konflikt, Settings-Symlink im Docker-Mount, enabledPlugins Array→dict)
- **2026-04-07** — MC V2 Spec + Plan finalisiert. 6 Feature-Trains implementiert (Backend-Patches, Frontend-Cleanup, Docker Image, HTTP-Poll Queue, PTY Terminal, Worker.sh PID-Lock)
- **2026-05-15** — M.4 3D Jarvis-Graph + Voice Tools (feature/vault-memory-foundation). `/memory/graph` Route (T10) assembliert MemoryGraph3D (Three.js, react-force-graph-3d, forwardRef), ClusterOverlay (SVG RAF), GraphFilterSidebar, NoteSidePanel, VoiceHighlightBridge, TraversalAnimation. Neue Hooks: `useVaultGraph` (TanStack Query, 60s staleTime), `useVaultStream` (WS live-invalidate), `useVoiceHighlight` (30s auto-clear). api.ts: `vault.graph()` ergaenzt. Sidebar: Network-Icon-Link `/memory/graph` nach Brain-Icon. ARCHITECTURE.md Vault-Sektion mit M.2/M.3/M.4 Status-Eintraegen aktualisiert. Kein Schema-Change, kein neues ADR (frontend-only Assembly-Task).
- **2026-05-14** — M.1 Read Foundation fuer Vault-as-Source landed (Spec `8226e8ba`, Plan `bbf03fe1`, 13 Tasks auf `feature/vault-memory-foundation` Branch). Neue Services: VaultIndex (SQLite FTS5), VaultActivity (Redis Heatmap), VaultGit (Stub), VaultEmbeddings (No-op Stub), VaultWatcher (watchfiles FS-Watcher + Quarantaene). Neue Routen: `GET /api/v1/vault/notes`, `/search`, `/note/{path}`. Neue Scopes: `vault:read`, `vault:write`. ADR-034 (Proposed).
- **2026-03-xx** — Subagent-Dispatch (isolated Sessions) als Default. Kill-Switch `USE_SUBAGENT_DISPATCH=false` für Rollback
- **2026-02-xx** — Dispatch ACK Handshake (Migration 0018). Verhindert Task-Verlust nach Agent-Restart
- **2026-02-xx** — Agent Help Requests + Clarification + Structured Blockers
- **2026-01-xx** — Credentials Vault (Fernet-verschlüsselt, Migration 0067)
- **2026-01-xx** — Unified Plugin Management (Shared Cache, DB-Zuweisung, Skills UI)
