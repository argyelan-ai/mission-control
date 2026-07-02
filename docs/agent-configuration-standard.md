# Agent Configuration Standard

> **Golddokument.** Dieses Dokument beschreibt die verbindliche Konfiguration aller Agent-Rollen im Mission Control Fleet. Jede Änderung an Scopes, Plugins oder MCPs muss hier dokumentiert werden. Authoritative Quelle für Scopes: `backend/app/scopes.py` `DEFAULT_SCOPES`.

**Erstellt:** 2026-04-28
**Stand:** Phase 11 — MCP Assignment Fixes + Config Standard
**Verwandte ADRs:** ADR-013 (Settings SSoT), ADR-016 (MCP Management), ADR-025 (dispatch split)

---

## Übersicht

Der MC Fleet besteht aus 10 aktiven Agents in 8 verschiedenen Rollen. Jede Rolle hat definierte:

1. **Scopes** — welche API-Endpoints der Agent nutzen darf (durchgesetzt via TOOLS.md + `require_scope()`)
2. **CLI Plugins** — welche Claude Code Plugins im `settings.json` aktiviert sind
3. **MCP Server** — welche Model Context Protocol Server dem Agent zugewiesen sind
4. **Runtime** — wie der Agent gestartet und gemanagt wird

Ziel dieses Dokuments: Jeder Operator oder Entwickler kann die Frage "Was bekommt ein Tester-Agent?" ohne Reverse Engineering beantworten.

---

## Geltungsbereich

| Bereich | Gilt für |
|---------|---------|
| Scopes | Alle Agents, alle Runtimes |
| CLI Plugins | Nur CLI-Bridge und Host-Agents (claude binary) |
| MCP Server | Nur Agents mit claude oder openclaude binary |
| SOUL.md | Alle Agents (verschiedene Render-Mechanismen je Runtime) |

Henry (relay, OpenClaw Gateway) ist eine Sonderrolle — MCP-Konzepte gelten nicht.

---

## Rollenarchitektur

| Rolle (AgentRole enum) | Zweck | Aktive Agents |
|------------------------|-------|---------------|
| `orchestrator` | Autonomer Orchestrator, verteilt Tasks, trifft Entscheidungen | Boss |
| `relay` | OpenClaw Gateway Relay, Frontdoor für den Operator | Henry |
| `reviewer` | Code-Review, Security, Qualitätssicherung | Rex |
| `writer` | Content-Erstellung, Dokumentation, Video/Grafik | Davinci, Shakespeare |
| `developer` | Feature-Implementierung, Bugfixing | FreeCode, Sparky |
| `tester` | QA, Browser-Automatisierung, E2E-Tests | Tester |
| `deployer` | Deployment zu Vercel, Docker, Infra | Deployer |
| `researcher` | Recherche, Knowledge-Synthesis, Lessons | Researcher |
| `planner` | **RETIRED** — siehe unten | — |

---

## Konfigurationsschichten

Jeder Agent hat vier unabhängige Konfigurationsschichten:

**1. SOUL.md (Identität + Lifecycle-Regeln)**
Gerendert via `backend/templates/SOUL.md.j2` — Jinja2-Template mit rollenspezifischen Branches. Niemals direkt in der DB editieren — wird beim nächsten Reprovision überschrieben. Änderung: Template bearbeiten → Backend rebuild → sync-config.

**2. Scopes (API-Zugriff)**
Gespeichert als JSON-Array in `agents.scopes` (DB). Steuert TOOLS.md-Generierung und Backend `require_scope()` Checks. Authoritative Quelle: `DEFAULT_SCOPES` in `backend/app/scopes.py`. Leeres Array `[]` = ALL_SCOPES (Backward-Compat).

**3. CLI Plugins (Claude Code Erweiterungen)**
Gespeichert als JSON-Array in `agents.cli_plugins` (DB). Gerendert in `settings.json` als `enabledPlugins` dict. Shared Cache unter `~/.openclaw/plugins/`. Änderung via `PATCH /api/v1/agents/{id}/skills` mit `update_cli_plugins: true`.

**4. MCP Server (Model Context Protocol)**
Gespeichert als JSON-Array in `agents.mcp_servers` (DB). Gerendert in `~/.mc/agents/{slug}/claude-config/.claude.json`. Drei installierte Server: `filesystem`, `higgsfield`, `playwright`. Null = alle (Legacy-Bug — immer explizit setzen).

---

## Rolle: orchestrator

### Zweck

Autonomer Top-Level Orchestrator. Boss empfängt Tasks vom Operator oder Henry, erstellt Subtasks, koordiniert das Worker-Team und trifft Architekturentscheidungen. Implementiert nie selbst — delegiert immer.

### Scopes

Boss hat ALL_SCOPES (alle 18 Scopes) — identisch mit dem `lead`-Default aus Backward-Compat-Gründen:

- `tasks:read` — Task-Lesezugriff
- `tasks:write` — Task-Statusänderungen
- `tasks:create` — Neue Tasks erstellen
- `tasks:manage` — Task-Administration
- `knowledge:read` — Knowledge Base lesen
- `knowledge:write` — Knowledge Base schreiben
- `memory:read` — Board-Memory lesen
- `memory:write` — Board-Memory schreiben
- `approvals:create` — Approval-Anfragen stellen
- `chat:write` — Chat-Nachrichten senden
- `agents:manage` — Agents erstellen und verwalten
- `content:submit` — Content einreichen
- `heartbeat` — Heartbeat senden
- `deploy:execute` — Deployments starten
- `project:read` — Projekt-Metadaten lesen
- `project:write` — Projekt-Metadaten schreiben
- `tasks:help` — Help Requests stellen
- `credentials:read` — Credentials Vault lesen

### CLI Plugins

```
code-review, discord, firecrawl, frontend-design, playground, playwright,
skill-creator, supabase, superpowers, ui-ux-pro-max, vercel,
voltagent-core-dev, voltagent-meta, voltagent-research,
document-skills, example-skills, claude-api, github
```

Boss ist der vollständig ausgestattete Orchestrator — bekommt alle verfügbaren Plugins.

### MCP Server

```
[] (leer — kein MCP)
```

**Warum kein MCP:** Boss läuft als Host-Prozess (launchd), nicht in einem Docker-Container. Der MCP-Sync-Mechanismus (`PATCH mcp-servers`) schreibt in `~/.mc/agents/boss/claude-config/.claude.json`, nicht in das aktive `boss-host/`-Workspace-Verzeichnis. Boss verwendet direkte CLI-Befehle und Subagent-Delegation statt MCP-Tools.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "orchestrator" %}
```

Boss-spezifische Identität: autonomer Orchestrator, Entscheidungsträger, kein direktes Implementieren.

### Aktive Agents

| Agent | Modell | Workspace |
|-------|--------|-----------|
| Boss | claude-opus-4-7 | `~/.mc/agents/boss-host/claude-config/` |

### Runtime-Notizen

- **Runtime:** `host` — kein Docker, läuft direkt auf dem Mac Mini
- **Start:** `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.boss.plist`
- **Stop:** `launchctl bootout gui/$(id -u) com.openclaw.boss`
- **Aktiver Workspace:** `boss-host/` (nicht `boss/` — legacy-Verzeichnis wird nicht mehr genutzt)
- **sync-config:** Schlägt mit 400 fehl (`No gateway configured`) — Boss hat `gateway_id=null`. Config-Änderungen direkt in `boss-host/`-Files
- **DB-Sync:** `PATCH mcp-servers` schreibt in `boss/claude-config/` (falscher Pfad) — bei Boss direkt ignorieren
- **Neustart nach Config-Änderung:** `launchctl bootout` + `launchctl bootstrap`

---

## Rolle: relay

### Zweck

OpenClaw Gateway Relay. Henry ist die Frontdoor für den Operator — empfängt Anweisungen via Telegram/Chat, leitet an das Agent-Team weiter, aggregiert Ergebnisse. Kein lokaler Prozess — läuft ausschliesslich im OpenClaw Gateway.

### Scopes

Henry hat ALL_SCOPES (alle 18 Scopes) — identisch wie orchestrator:

- `tasks:read`, `tasks:write`, `tasks:create`, `tasks:manage`
- `knowledge:read`, `knowledge:write`
- `memory:read`, `memory:write`
- `approvals:create`, `chat:write`, `agents:manage`, `content:submit`
- `heartbeat`, `deploy:execute`, `project:read`, `project:write`
- `tasks:help`, `credentials:read`

### CLI Plugins

```
[] (keine — nicht anwendbar)
```

Henry hat keinen lokalen `claude`-Prozess. CLI-Plugin-Konzepte gelten nicht für den Gateway-Runtime.

### MCP Server

```
N/A (nicht anwendbar)
```

MCP ist ein Claude Code / claude-binary Konzept. Henry läuft im OpenClaw Gateway, der keinen lokalen LLM-Client verwaltet. `agents.mcp_servers = []` in der DB, aber der Wert wird nie gerendert oder angewendet.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "relay" %}
```

Henry-spezifische Identität: persönliche rechte Hand vom Operator, Koordinator, Kommunikations-Hub.

### Aktive Agents

| Agent | Modell | Gateway Agent ID |
|-------|--------|-----------------|
| Henry | OpenClaw Gateway (kein lokales Modell) | `main` |

### Runtime-Notizen

- **Runtime:** `openclaw` — kein lokales Binary, kein `.claude.json`, kein `settings.json`
- **SOUL.md Sync:** Via Gateway RPC (`rpc.agents_files_set("SOUL.md", content)`) — Gateway verwaltet den System-Prompt
- **sync-config:** Funktioniert via OpenClaw-Pfad — pusht SOUL.md via RPC
- **Nach sync-config:** Kein Container-Restart nötig — Gateway lädt SOUL.md automatisch
- **Keine Disk-Files:** `~/.mc/agents/henry/` existiert auf dem Host nicht — keine claude-config, keine Plugins

---

## Rolle: reviewer

### Zweck

Code-Review, Security-Analyse und Qualitätssicherung. Rex prüft die Arbeit der Developer-Agents, gibt strukturiertes Feedback und setzt Tasks auf `done` (Approve) oder `in_progress` (Request Changes).

### Scopes

Aus `DEFAULT_SCOPES[AgentRole.REVIEWER]` in `scopes.py`:

- `tasks:read` — Tasks und ihre Details lesen
- `tasks:write` — Task-Status ändern (review → done / in_progress)
- `knowledge:read` — Projektdokumentation lesen
- `knowledge:write` — Review-Erkenntnisse dokumentieren
- `memory:read` — Frühere Lessons und Entscheidungen lesen
- `approvals:create` — Approval-Anfragen an den Operator stellen
- `chat:write` — Feedback-Kommentare schreiben
- `heartbeat` — Heartbeat senden
- `tasks:help` — Help Requests stellen

**Nicht vorhanden (bewusst):** `memory:write`, `tasks:create`, `agents:manage`, `deploy:execute`, `credentials:read`, `project:read/write`

Rex dokumentiert Erkenntnisse in Knowledge (`knowledge:write`), schreibt aber nicht in den Board-Memory. Task-Erstellung und Agent-Management sind Orchestrator-Domäne.

### CLI Plugins

```
code-review, discord, firecrawl, playground, superpowers, skill-creator,
document-skills, example-skills, github, playwright
```

Code-Review-Plugin für strukturiertes Review-Feedback. playwright für visuelle Verifikation von Frontend-Änderungen. Discord für direkte Kommunikation.

### MCP Server

```
["filesystem"]
```

**Warum filesystem:** Rex liest häufig lokale Projektdateien direkt für Code-Review — direkter Dateizugriff via MCP ist effizienter als `cat`-Befehle.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "reviewer" %}
```

Rex-spezifische Identität: kritisch, präzise, sicherheitsbewusst. Kennt gute Code-Qualität.

### Aktive Agents

| Agent | Modell | Container |
|-------|--------|-----------|
| Rex | claude-sonnet-4-6 | `mc-agent-rex` |

### Runtime-Notizen

- **Runtime:** `cli-bridge` — Docker-Container, `mc-claude-agent:latest` Image
- **Binary:** `claude` (native Anthropic CLI) + `CLAUDE_CODE_OAUTH_TOKEN`
- **Workspace:** `~/.mc/agents/rex/claude-config/` (bind-gemountet als `/home/agent/.claude/`)
- **sync-config:** Läuft via `docker_agent_sync.sync_docker_agent_files()` — schreibt direkt in den Bind-Mount
- **Container-Neustart:** Nach `PATCH mcp-servers` muss der Container neu gestartet werden, damit `.claude.json` eingelesen wird

---

## Rolle: writer

### Zweck

Content-Erstellung, Dokumentation, Blog-Posts, Video-Skripte und Grafik-Produktion. Zwei spezialisierte Agents: Davinci (Video/Grafik), Shakespeare (Text/Content).

### Scopes

Aus `DEFAULT_SCOPES[AgentRole.WRITER]` in `scopes.py`:

- `knowledge:read` — Projektdokumentation und Referenz-Material lesen
- `memory:read` — Frühere Content-Entscheidungen nachlesen
- `chat:write` — Chat-Kommunikation
- `content:submit` — Finalen Content einreichen
- `heartbeat` — Heartbeat senden
- `tasks:help` — Help Requests stellen

**Nicht vorhanden (bewusst):** `tasks:write`, `tasks:create`, `knowledge:write`, `memory:write`, `agents:manage`, `deploy:execute`, `credentials:read`, `project:read/write`

Writer-Agents sind reine Content-Produzenten. Sie lesen Kontext, erstellen Content und reichen ein — keine Systemzugriffe, keine Task-Status-Änderungen ausser via Orchestrator-Delegation.

### CLI Plugins

**Davinci (Video/Grafik-Spezialist):**
```
frontend-design, playground, skill-creator, superpowers, ui-ux-pro-max,
code-review, discord, github, firecrawl, remotion-superpowers,
document-skills, example-skills
```

Davinci hat `remotion-superpowers` — spezialisiertes Plugin für programmatische Video-Erstellung (Remotion Framework). `ui-ux-pro-max` und `frontend-design` für visuelle Aufgaben.

**Shakespeare (Text/Content-Spezialist):**
```
github, skill-creator, firecrawl, superpowers, document-skills, example-skills
```

Shakespeare hat einen schmaleren Plugin-Set — fokussiert auf Research (firecrawl), GitHub (Content-Publishing) und Standard-Utilities.

### MCP Server

```
[] (leer)
```

**Warum kein MCP:** Writer-Agents erstellen Content via natürliche Sprache und CLI-Tools. Direkter Filesystem- oder Browser-Automation-Zugriff ist kein Teil des Writer-Workflows.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "writer" %}
```

Writer-spezifische Identität: kreativ, stilbewusst. Unterschiedliche Personas je Agent (Davinci = Künstler, Shakespeare = Schreiber).

### Aktive Agents

| Agent | Spezialisierung | Modell | Container |
|-------|----------------|--------|-----------|
| Davinci | Video, Grafik, UI/Motion | claude-sonnet-4-6 | `mc-agent-davinci` |
| Shakespeare | Texte, Dokumentation, Content | claude-sonnet-4-6 | `mc-agent-shakespeare` |

### Runtime-Notizen

- **Runtime:** `cli-bridge` — Docker-Container, `mc-claude-agent:latest` Image
- **Binary:** `claude` (native Anthropic CLI) + `CLAUDE_CODE_OAUTH_TOKEN`
- **Workspace je Agent:** `~/.mc/agents/davinci/claude-config/` und `~/.mc/agents/shakespeare/claude-config/`
- **Hinweis:** `remotion-superpowers` Plugin ist nur bei Davinci — bei neuen Writer-Agents prüfen ob das Plugin benötigt wird

---

## Rolle: developer

### Zweck

Feature-Implementierung, Bugfixing, Code-Änderungen. Developer-Agents empfangen konkrete Implementierungs-Tasks, arbeiten in Git-Branches und setzen Status auf `review` wenn fertig.

### Scopes

Aus `DEFAULT_SCOPES[AgentRole.DEVELOPER]` in `scopes.py`:

- `tasks:read` — Tasks und ihre Details lesen
- `tasks:write` — Task-Status ändern (in_progress → review/blocked/failed)
- `knowledge:read` — Projektdokumentation und API-Referenzen lesen
- `knowledge:write` — Lessons und Erkenntnisse dokumentieren
- `memory:read` — Board-Memory und frühere Entscheidungen lesen
- `memory:write` — Learnings in Board-Memory schreiben
- `approvals:create` — Approval-Anfragen für Architekturentscheidungen
- `chat:write` — Status-Updates und Fragen
- `heartbeat` — Heartbeat senden
- `project:read` — Projekt-Metadaten und GitHub-Repo-URL lesen
- `project:write` — Projekt-Status aktualisieren
- `tasks:help` — Help Requests stellen
- `credentials:read` — API-Keys und Secrets aus dem Credentials Vault lesen

**Nicht vorhanden:** `tasks:create`, `agents:manage`, `deploy:execute`

Developer erstellen keine Tasks selbst (Orchestrator-Domäne) und deployen nicht selbst (Deployer-Domäne).

### CLI Plugins

**FreeCode (Allrounder Developer):**
```
superpowers, github, skill-creator, code-review, frontend-design, playground,
document-skills, example-skills, claude-api, playwright
```

**Sparky (Workhorse Developer, lokales LLM):**
```
code-review, discord, firecrawl, frontend-design, github, skill-creator,
supabase, superpowers, ui-ux-pro-max, voltagent-research,
document-skills, example-skills, claude-api, playwright
```

Sparky hat `supabase` und `voltagent-research` — spezialisierter für Backend-Infrastruktur und Research-Aufgaben.

### MCP Server

```
[] (leer)
```

**Warum kein MCP:** Developer-Agents arbeiten via `claude` CLI mit Editor-Integration (Read/Write/Edit Tools). playwright-MCP ist für Browser-Automation (Tester-Domäne). Direkter Filesystem-Zugriff via MCP würde mit Claude's nativen File-Tools konkurrieren.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "developer" %}
```

Developer-spezifische Identität: pragmatisch, lösungsorientiert, kennt die MC-Codebase.

### Aktive Agents

| Agent | Spezialisierung | Modell | Container |
|-------|----------------|--------|-----------|
| FreeCode | Allrounder, Full-Stack | claude-sonnet-4-6 | `mc-agent-freecode` |
| Sparky | Workhorse, Backend/Infra | qwen3-coder-next (LM Studio/Ollama) | `mc-agent-sparky` |

### Runtime-Notizen

- **FreeCode Runtime:** `docker` (`mc-agent-base:latest`, openclaude shim auf claude sonnet) — `exec openclaude --append-system-prompt`
- **Sparky Runtime:** `docker` (`mc-agent-base:latest`, openclaude + OpenAI-Shim → LM Studio) — `exec openclaude --append-system-prompt`
- **Workspace FreeCode:** `~/.mc/agents/freecode/claude-config/`
- **Workspace Sparky:** `~/.mc/agents/sparky/claude-config/`
- **sync-config Sparky:** Routing über OpenClaw-Pfad via `gateway_agent_id="spark"` — pusht SOUL.md via RPC. `.claude.json` wird via `PATCH mcp-servers` direkt in den Bind-Mount geschrieben
- **sync-config FreeCode:** Routing über OpenClaw-Pfad via `gateway_agent_id="free-code"` — beide sind im Gateway registriert

---

## Rolle: tester

### Zweck

QA-Automatisierung, Browser-Tests, End-to-End-Tests. Tester-Agents führen Test-Suites aus, erstellen Playwright-Tests und verifizieren visuell Deployments.

### Scopes

Aus `DEFAULT_SCOPES[AgentRole.TESTER]` in `scopes.py`:

- `tasks:read` — Tasks und Test-Anforderungen lesen
- `tasks:write` — Task-Status nach Test-Abschluss setzen
- `knowledge:read` — Bestehende Test-Dokumentation lesen
- `knowledge:write` — Test-Ergebnisse und Erkenntnisse dokumentieren
- `memory:write` — Test-Learnings in Board-Memory schreiben
- `chat:write` — Test-Reports kommunizieren
- `heartbeat` — Heartbeat senden
- `tasks:help` — Help Requests stellen
- `credentials:read` — Test-Accounts und API-Keys aus dem Credentials Vault

**Nicht vorhanden:** `memory:read`, `tasks:create`, `agents:manage`, `deploy:execute`, `project:read/write`

Tester schreiben Erkenntnisse (`memory:write`) lesen aber nicht systematisch den gesamten Memory-Kontext.

### CLI Plugins

```
superpowers, github, skill-creator, code-review, playwright, document-skills, example-skills
```

`playwright` Plugin für Browser-Automatisierung und Snapshot-Tests.

### MCP Server

```
["playwright"]
```

**Warum playwright MCP:** Tester-Agents führen Browser-Automatisierung als primäre Aufgabe aus. Das playwright MCP server (`mcr.microsoft.com/playwright/mcp`, HTTP-Transport) ermöglicht Browser-Kontrolle via MCP-Protokoll — effizienter als CLI-Commands für komplexe UI-Tests.

**Transport:** HTTP (Docker-Container `playwright/mcp`). Der Container muss erreichbar sein wenn Tester läuft.

**Warum nicht filesystem:** Tester liest Test-Code via Claude's native File-Tools. Direkter MCP-Filesystem-Zugriff ist nicht notwendig.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "tester" %}
```

Tester-spezifische Identität: methodisch, Detail-orientiert, kennt Testing Best Practices.

### Aktive Agents

| Agent | Modell | Container |
|-------|--------|-----------|
| Tester | claude-sonnet-4-6 | `mc-agent-tester` |

### Runtime-Notizen

- **Runtime:** `cli-bridge` — Docker-Container, `mc-claude-agent:latest` Image
- **Binary:** `claude` (native Anthropic CLI) + `CLAUDE_CODE_OAUTH_TOKEN`
- **Workspace:** `~/.mc/agents/tester/claude-config/`
- **playwright MCP Voraussetzung:** `mcr.microsoft.com/playwright/mcp` Docker-Image muss laufen und vom Tester-Container erreichbar sein
- **Nach `PATCH mcp-servers`:** Container-Neustart erforderlich (`docker compose -f docker/docker-compose.agents.yml restart mc-agent-tester`)

---

## Rolle: deployer

### Zweck

Deployment-Automation für Vercel, Docker und andere Infrastruktur. Deployer-Agents führen Deployment-Befehle aus, monitoren den Deploy-Status und berichten Ergebnisse.

### Scopes

Aus `DEFAULT_SCOPES[AgentRole.DEPLOYER]` in `scopes.py`:

- `tasks:read` — Tasks und Deployment-Anforderungen lesen
- `tasks:write` — Task-Status nach Deployment setzen
- `knowledge:read` — Deployment-Dokumentation und Runbooks lesen
- `knowledge:write` — Deployment-Ergebnisse dokumentieren
- `memory:write` — Deployment-Learnings festhalten
- `chat:write` — Deploy-Status kommunizieren
- `heartbeat` — Heartbeat senden
- `deploy:execute` — Deployments starten (z.B. `POST /api/v1/deployments`)
- `tasks:help` — Help Requests stellen
- `credentials:read` — Deployment-Credentials (Vercel-Token, etc.) aus dem Vault

**Nicht vorhanden:** `memory:read`, `tasks:create`, `agents:manage`, `project:read/write`

`deploy:execute` ist der exklusive Scope für Deployment-Operationen — nur Deployer-Agents haben diesen Zugriff.

### CLI Plugins

```
superpowers, github, skill-creator, vercel, document-skills
```

`vercel` Plugin für Vercel-Deployments. `github` für Deployment-Branches und Release-Management.

### MCP Server

```
[] (leer)
```

**Warum kein MCP:** Deployer nutzt Vercel CLI, GitHub CLI (`gh`) und Docker CLI — alles via Shell-Commands in Claude's Bash-Tool. MCP-Tools würden keinen Mehrwert bringen und erweitern unnötig die Angriffsfläche.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "deployer" %}
```

Deployer-spezifische Identität: präzise, versteht Deployment-Risiken, bestätigt vor irreversiblen Operationen.

### Aktive Agents

| Agent | Modell | Container |
|-------|--------|-----------|
| Deployer | claude-sonnet-4-6 | `mc-agent-deployer` |

### Runtime-Notizen

- **Runtime:** `cli-bridge` — Docker-Container, `mc-claude-agent:latest` Image
- **Binary:** `claude` (native Anthropic CLI) + `CLAUDE_CODE_OAUTH_TOKEN`
- **Workspace:** `~/.mc/agents/deployer/claude-config/`
- **Vercel-Auth:** Via `credentials:read` + Credentials Vault (kein hardcodierter Token)
- **GitHub-Auth:** Via `GH_TOKEN` Env-Var im Container

---

## Rolle: researcher

### Zweck

Research, Wissens-Synthese und Lesson-Extraktion. Researcher-Agents führen tiefe Recherchen durch, schreiben Knowledge Base Einträge und destillieren Erkenntnisse in strukturierte Lessons.

### Scopes

Aus `DEFAULT_SCOPES[AgentRole.RESEARCHER]` in `scopes.py`:

- `knowledge:read` — Bestehendes Wissen lesen (Knowledge Base, Board Memory)
- `knowledge:write` — Research-Ergebnisse in Knowledge Base schreiben
- `memory:read` — Frühere Lessons nachschlagen (für mc memory search, Reflection-Flow)
- `memory:write` — Neue Lessons in Board-Memory schreiben
- `chat:write` — Research-Berichte kommunizieren
- `content:submit` — Content-Artefakte einreichen
- `heartbeat` — Heartbeat senden
- `project:read` — Projekt-Kontext für Research verstehen
- `project:write` — Research-Erkenntnisse in Projekt-Dokumentation schreiben
- `tasks:help` — Help Requests stellen

**Hinweis:** `memory:read` wurde in Phase v0.5 (2026-04-23) zu Researcher hinzugefügt. Vorher schlug `mc memory search` mit 403 fehl — der Lesson-Reflection-Loop war kaputt. Nie wieder entfernen.

**Nicht vorhanden:** `tasks:write`, `tasks:create`, `agents:manage`, `deploy:execute`, `approvals:create`, `credentials:read`

### CLI Plugins

```
superpowers, github, skill-creator, firecrawl, voltagent-research, document-skills
```

`firecrawl` für Web-Scraping und Research. `voltagent-research` für strukturierte Research-Workflows.

### MCP Server

```
[] (leer)
```

**Warum kein MCP:** Researcher nutzt `firecrawl` Plugin und Web-Search-Befehle für Research. MCP filesystem ist nicht notwendig — Researcher liest externe Quellen, nicht lokale Codebases. MCP-Zugriff würde den Research-Scope unangemessen erweitern.

### SOUL.md Template

Branch in `backend/templates/SOUL.md.j2`:
```jinja2
{% elif agent.role == "researcher" %}
```

Researcher-spezifische Identität: analytisch, quellenorientiert, strukturiert.

### Aktive Agents

| Agent | Modell | Container |
|-------|--------|-----------|
| Researcher | claude-sonnet-4-6 | `mc-agent-researcher` |

### Runtime-Notizen

- **Runtime:** `cli-bridge` — Docker-Container, `mc-claude-agent:latest` Image
- **Binary:** `claude` (native Anthropic CLI) + `CLAUDE_CODE_OAUTH_TOKEN`
- **Workspace:** `~/.mc/agents/researcher/claude-config/`

---

## Rolle: planner (RETIRED)

### Warum retired

Die `planner` Rolle und alle Planner-Agents wurden in **Phase 9 (2026-04-28)** entfernt.

**Hintergrund:** Ein dedizierter Planner-Agent war für strukturierte Phasen-Planung zuständig. Die Rolle erwies sich als Bottleneck: Boss (orchestrator) hat denselben Planungs-Kontext und alle notwendigen Scopes. Ein separater Planner erzeugte einen unnötigen Dispatch-Hop ohne Mehrwert.

**Aktueller Stand:**
- Kein aktiver Agent hat `role = "planner"` in der DB
- Ghost-Agents "Planner" und "Neo" wurden entfernt (nicht mehr in DB)
- `AgentRole.PLANNER` enum und `DEFAULT_SCOPES[AgentRole.PLANNER]` bleiben in `scopes.py` erhalten (Backward-Compat für bestehende Scopes-Checks)
- `_find_planning_agent()` in `dispatch.py` prüft noch auf Planner — falls kein Planner gefunden, fällt es auf Board Lead (Henry) zurück

**Scopes die Planner hatte (historisch):**
`tasks:read`, `knowledge:read/write`, `memory:write`, `approvals:create`, `chat:write`, `heartbeat`, `project:read/write`, `tasks:help`

**Wenn ein neuer Planner-Agent gewünscht wird:** Neuen Agent mit `role="planner"` erstellen — das Template und die Scopes sind noch vorhanden. Boss übernimmt derzeit die Planungs-Funktion.

---

## Konfiguration ändern — Workflows

### 1. Scopes ändern

Scopes werden in der DB gespeichert und über TOOLS.md an den Agent gerendert.

```bash
# Option A: Via API (empfohlen)
curl -X PATCH http://localhost/api/v1/agents/{agent_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"scopes": ["tasks:read", "tasks:write", "knowledge:read", ...]}'

# Danach: sync-config damit TOOLS.md neu generiert wird
curl -X POST http://localhost/api/v1/agents/{agent_id}/sync-config \
  -H "Authorization: Bearer $TOKEN"

# Option B: Direkt in DB (nur für Notfälle)
docker compose exec db psql -U mc mission_control
UPDATE agents SET scopes = '["tasks:read", "tasks:write"]' WHERE name = 'Rex';
```

**Nach der Änderung:** sync-config erzeugt neues TOOLS.md. Container-Neustart ist NICHT zwingend nötig (TOOLS.md wird per Append-System-Prompt beim nächsten claude-Start geladen).

### 2. CLI Plugins ändern

```bash
# Via API — rendert settings.json neu + startet Worker neu
curl -X PATCH http://localhost/api/v1/agents/{agent_id}/skills \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "update_cli_plugins": true,
    "cli_plugins": ["superpowers@claude-plugins-official", "github@claude-plugins-official"]
  }'
```

Plugin-Keys haben das Format `{name}@{publisher}`. Alle installierten Plugins: `GET /api/v1/plugins`.

**Wichtig:** `enabledPlugins` in `settings.json` MUSS ein dict sein, kein Array. `plugin_manager.py` rendert korrekt — nie manuell in `settings.json` editieren.

### 3. MCP Server ändern

```bash
# Setzen (überschreibt komplett)
curl -X PATCH http://localhost/api/v1/agents/{agent_id}/mcp-servers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mcp_servers": ["playwright"]}'

# Zurücksetzen auf leer
curl -X PATCH http://localhost/api/v1/agents/{agent_id}/mcp-servers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mcp_servers": []}'
```

**Nach `PATCH mcp-servers`:** Container-Neustart zwingend erforderlich — claude binary liest `.claude.json` nur beim Start.

```bash
docker compose -f docker/docker-compose.agents.yml restart mc-agent-{slug}
```

**Verfügbare MCP-Namen:** `filesystem`, `higgsfield`, `playwright`
**Semantik:** `null` = alle (Legacy-Bug), `[]` = keine, `["name"]` = explizit (empfohlen)

### 4. SOUL.md ändern

```bash
# 1. Template bearbeiten (NIEMALS direkt DB-Edit)
vi backend/templates/SOUL.md.j2

# 2. Backend rebuild
docker compose up --build -d backend

# 3. sync-config um neue SOUL.md zu pushen
curl -X POST http://localhost/api/v1/agents/{agent_id}/sync-config \
  -H "Authorization: Bearer $TOKEN"

# Bei Henry: PUT /config/soul_md + sync-config (da kein --append-system-prompt)
curl -X PUT http://localhost/api/v1/agents/{henry_id}/config/soul_md \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"soul_md": "..."}'
```

**Absolutes Verbot:** Nie direkte DB-Edits für soul_md — werden beim nächsten Reprovision überschrieben.

### 5. Neuen Agent einer Rolle hinzufügen

```bash
# 1. Agent erstellen (setzt automatisch DEFAULT_SCOPES für die Rolle)
curl -X POST http://localhost/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "NewAgent", "role": "developer", "model": "claude-sonnet-4-6"}'

# 2. Provision (erstellt Workspace, SOUL.md, TOOLS.md)
curl -X POST http://localhost/api/v1/agents/{id}/provision \
  -H "Authorization: Bearer $TOKEN"

# 3. Plugins zuweisen (optional, Standard = alle)
# 4. MCP setzen (immer explizit setzen — nie null lassen)
curl -X PATCH http://localhost/api/v1/agents/{id}/mcp-servers \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"mcp_servers": []}'
```

---

## Änderungshistorie

| Datum | Version | Änderung |
|-------|---------|---------|
| 2026-04-28 | 1.0 | Erstellt im Rahmen von Phase 11 (AUD-07). 8 aktive Rollen + planner retired dokumentiert. MCP-Zielzustand nach Phase 11 festgehalten. |
