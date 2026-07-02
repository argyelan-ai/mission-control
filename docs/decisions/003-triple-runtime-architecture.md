# ADR-003 — Triple-Runtime-Architektur

**Status:** Accepted
**Datum:** 2026-04-07
**Scope:** Infra/Runtime

## Kontext

Im Laufe der Zeit sind in MC drei unterschiedliche Agent-Runtimes entstanden:

1. **openclaw (V1)** — Gateway-basiert, WebSocket RPC, Session-getrieben. Ursprüngliche Architektur für Claude Code CLI auf Host.
2. **cli-bridge (Host-Side)** — Host-Worker via `worker.sh` + tmux + File-Queue in `~/.openclaw/agents/{name}/queue/pending/`. Entstanden für openclaude/openclaude mit lokalen Models (LM Studio).
3. **Docker V2** — Docker-Container pro Agent, HTTP-Poll via `poll.sh`, tmux im Container, PTY-Proxy ins Browser-Terminal.

Die Frage war: **Auf eine Runtime konsolidieren oder alle parallel betreiben?**

Gründe gegen "nur eine":
- **Risk Appetite**: Komplette Migration mit Downtime ist zu riskant (der Operator arbeitet täglich damit)
- **Boss vs Rest**: Boss braucht echten `claude`-Binary für Orchestrator-Rolle (Anthropic API, nicht openclaude)
- **Legacy-Gateway-Agents**: Bestehende Sessions/Chats sollten nicht verloren gehen
- **Sicherheits-Isolation**: Docker gibt Container-Isolation die Host-cli-bridge nie hatte

## Entscheidung

**Alle drei Runtimes parallel laufen lassen.** Unterscheidung über:
- `agent.agent_runtime` DB-Feld: `openclaw` | `cli-bridge`
- Docker V2 Agents haben `agent_runtime = 'cli-bridge'` aber zusätzlich einen laufenden `mc-agent-{slug}` Docker-Container
- Runtime-Detection in Routern: `docker ps` Check im `/docker-sessions/agents` Endpoint

**Dispatch-Router** (`find_dispatch_target()` in `dispatch.py`) wählt den richtigen Weg:
- openclaw → `rpc.chat_send()` via WebSocket Gateway
- cli-bridge (Host) → File-Queue in `~/.openclaw/agents/{slug}/queue/`
- cli-bridge (Docker V2) → HTTP-Poll, Agent selber holt Tasks via `GET /api/v1/agent/me/next-task`

**Konfig-Rendering** (cli-bridge.py) ist **shared** für Host und Docker V2 — dieselben Jinja2-Templates, dieselben `settings.json`/`agent.env`. Volume-Mount mountet `~/.openclaw/agents/{slug}/claude-config` → `/home/agent/.claude`.

## Alternativen

- **A: Alle auf Docker V2 migrieren** → verworfen weil Gateway-Agents (Boss mit claude-binary) davon betroffen wären, Risiko zu gross
- **B: Host-cli-bridge beibehalten, Docker V2 verwerfen** → verworfen weil fehlende Container-Isolation, keine Browser-Sichtbarkeit ins Terminal
- **C: Gateway als Proxy für alle** → verworfen weil Gateway dann Single Point of Failure, zusätzliche Protokoll-Schicht
- **D: Komplett neue Runtime von Grund auf** → zu gross, zu riskant, keine offensichtliche Verbesserung

## Konsequenzen

### Positiv
- **Zero-Downtime-Migration möglich**: Agent für Agent umstellen, Rollback jederzeit per DB-Update
- **Rolle-spezifische Optimierung**: Boss auf claude-binary (Orchestrator-Qualität), Worker auf openclaude/Ollama (Kosten)
- **Sicherheits-Isolation** durch Docker für die meisten Agents
- **Browser-Terminal-Einsicht** für alle Docker V2 Agents (Live Debugging)
- **Kostensparend**: Docker V2 nutzt Ollama Cloud GLM (billiger als Anthropic)

### Negativ
- **3 Code-Paths in Dispatch**: Jeder neue Dispatch-Feature muss an 3 Stellen getestet werden
- **Template-Konsistenz**: Änderungen in `cli_agent_settings.json.j2` müssen auf Host UND Docker funktionieren (siehe Symlink-Bug, ADR-013)
- **Debugging komplexer**: "Welche Runtime hat dieser Agent?" muss immer im Kopf sein
- **Mehr Services**: Gateway-Prozess + cli-bridge.py + Docker-Container parallel → mehr Monitoring nötig
- **Dokumentations-Pflicht**: Ohne sorgfältige Doku (wie diese) verliert man den Überblick

## Referenzen

- Dispatch-Routing: `backend/app/services/dispatch.py:find_dispatch_target()`
- Docker V2 Implementation: `docker/mc-agent-base/`, `docker/docker-compose.agents.yml`
- CLI-Bridge: `scripts/cli-bridge.py` (Host HTTP Server :18792)
- Gateway: `backend/app/services/openclaw_rpc.py`, Host-Prozess auf Port 18789
- Runtime-Filter: `backend/app/routers/cli_terminal.py:list_docker_session_agents()`
- Verwandt: ADR-011 (HTTP-Polling), ADR-013 (Docker V2 Deployment-Lessons)
