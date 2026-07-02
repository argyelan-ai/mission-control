# ADR-014 — Boss runs as macOS host process (claude binary, OAuth)

**Status:** Accepted
**Datum:** 2026-04-17
**Scope:** Infra/Runtime

## Kontext

Boss (Henry) ist der zentrale Orchestrator von Mission Control — empfängt alle Tasks vom Operator, delegiert an Worker-Agents (Cody, Rex, etc.) und überwacht Phase-Completion. Bisher lief Boss als Docker-Container (`mc-agent-boss`) mit `openclaude` als Binary, das auf eine selbst-gehostete LLM-Instanz (GLM 5.1 via Ollama Cloud) routete.

Zwei Probleme häuften sich:

1. **Memory-Leak im Container**: Der `claude`/`openclaude`-Prozess wuchs auch im Idle-State um ~140 MB / 2 Tage (siehe `feedback_container_memory_leak.md`). Periodischer Restart war ein Workaround, fixte aber nicht die Ursache.
2. **Reasoning-Qualität**: GLM 5.1 reichte für Worker-Tasks, war aber für Boss-Orchestration zu schwach — fehlerhafte Phase-Detection, schwache Task-Decomposition, häufige Re-Prompts nötig. Der Operator wollte für Boss explizit **Opus 4.7** (höchste Modell-Qualität).

Anthropic-API-Kosten für Boss würden bei pay-as-you-go im aktuellen Volumen schnell skalieren. Der Operator hat bereits eine **Claude Max Subscription** (Flat-Rate) — der offizielle `claude` CLI-Binary nutzt diese OAuth-basiert ohne zusätzliche API-Kosten.

`claude` mit OAuth-Login funktioniert nur, wenn der Prozess Zugriff auf macOS Keychain hat (`~/.claude/`-Tokens). Im Docker-Container ist das nicht trivial mountbar — Keychain ist host-spezifisch und nicht für Container-Sharing designt.

## Entscheidung

Boss läuft als **macOS launchd-Job auf dem Host** (`com.openclaw.boss`), startet das offizielle Anthropic-`claude`-Binary mit OAuth-Login der Claude-Max-Subscription des Operators.

- **Lifecycle:** launchd → `~/.openclaw/agents/boss-host/entrypoint.sh` → tmux-Session `boss-host` mit zwei Windows (claude + poll.sh).
- **Modell:** `ANTHROPIC_MODEL=claude-opus-4-7`. Direkter `api.anthropic.com`-Call (KEIN openclaude/LM-Studio-Detour wie im Container).
- **Terminal-Visibility für Browser:** zweites launchd-Job `com.openclaw.boss-ttyd` startet ttyd auf 127.0.0.1:7681, das die tmux-Session als WebSocket exponiert. Backend hat einen neuen WS-Proxy-Endpoint `/api/v1/host-agents/{id}/terminal`, den die Frontend-Sessions-Page identisch zur Docker-Variante nutzt.
- **Task-Loop:** poll.sh in tmux Window 1 pollt `/api/v1/agent/me/poll` (lokal) → `tmux load-buffer` + `paste-buffer` → claude in Window 0 verarbeitet. Identisches Muster zu Docker-V2-Agents, nur ohne Container-Boundary.
- **DB-Marker:** Neuer Wert `agents.agent_runtime = 'host'` (Migration 0073). `docker_agent_sync.py` skippt diese Agents (kein Container-Lifecycle).
- **Authoritative Scripts** liegen im Repo (`docker/boss-host/`), Runtime-Kopien in `~/.openclaw/agents/boss-host/`. Setup-Doku: `docker/boss-host/README.md`.

## Alternativen

- **API-Key statt OAuth (Boss bleibt im Container):** Boss läuft weiter im Container, bekommt einen Anthropic-API-Key, ruft Opus 4.7 direkt an. → Verworfen weil pay-as-you-go-Kosten signifikant sind während der Operator bereits eine Flat-Rate-Subscription bezahlt. Memory-Leak wäre auch nicht gefixt.
- **Boss bleibt mit GLM 5.1 + periodischer Container-Restart:** Memory-Leak-Workaround beibehalten, GLM 5.1 für Boss akzeptieren. → Verworfen weil die Reasoning-Qualität für Orchestration zu schwach ist (das primäre Anliegen des Operators).
- **Boss im Container mit OAuth-Mount:** `~/.claude/`-Verzeichnis read-write in den Container mounten. → Verworfen weil macOS Keychain-Zugriff host-gebunden ist (nicht nur File-Tokens, auch System-API-Calls), und ein erfolgreicher OAuth-Flow im Container das Auth-Modell von Anthropic verletzt (Account-Sharing-Risiko).

## Konsequenzen

### Positiv
- Boss nutzt Opus 4.7 → deutlich bessere Orchestration-Qualität (Phase-Detection, Task-Decomposition, Recovery-Recap).
- Keine zusätzlichen API-Kosten — Claude Max Subscription deckt Boss vollständig ab.
- Memory-Leak-Issue ist effektiv moot: claude auf dem Host kann via `launchctl kickstart -k` jederzeit ohne Container-Rebuild restartet werden, und ein Host-Prozess der wächst ist trivial sichtbar in Activity Monitor.
- launchd übernimmt Auto-Restart bei Crash (KeepAlive). Kein eigener Watchdog nötig.
- Browser-Terminal funktioniert identisch zu Docker-Agents (gleiche Sessions-Page-UI), nur Backend-Proxy-Pfad ist anders.

### Negativ
- **Keine Container-Isolation mehr für Boss.** Boss-Prozess läuft mit User-Permissions von Henry-Account und kann auf das gesamte `~/Workspace/` zugreifen. Mitigation: SOUL-Prompt + manuelles Monitoring der Sessions-Page durch den Operator. Boss hat keinen Schreibzugriff auf System-Verzeichnisse (User-Account).
- **Boss-Setup ist macOS-spezifisch** (launchd plists). Auf einem Linux-Host müsste man systemd-units schreiben. Mitigation: Mac Mini ist eh single-host (war auch vorher schon), und die Setup-Doku in `docker/boss-host/README.md` macht den Reproduktionspfad explizit.
- **Pairing-Voraussetzung:** Backend muss MC erreichbar sein wenn launchd Boss startet (sonst poll-Loop läuft ins Leere). In der Praxis kein Problem, da Backend früher startet.
- **Boss-Recovery via DB-Reset funktioniert nicht** wie bei Container-Agents (Container-Recreate). Stattdessen: `launchctl unload && load`. Doku in `docker/boss-host/README.md`.

## Referenzen

- Repo-Scripts: [`docker/boss-host/entrypoint.sh`](../../docker/boss-host/entrypoint.sh), [`docker/boss-host/start-claude.sh`](../../docker/boss-host/start-claude.sh), [`docker/boss-host/poll.sh`](../../docker/boss-host/poll.sh)
- Launchd plists: [`docker/boss-host/com.openclaw.boss.plist`](../../docker/boss-host/com.openclaw.boss.plist), [`docker/boss-host/com.openclaw.boss-ttyd.plist`](../../docker/boss-host/com.openclaw.boss-ttyd.plist)
- Setup-Doku: [`docker/boss-host/README.md`](../../docker/boss-host/README.md)
- Migration: `backend/alembic/versions/0073_*.py` (`agent_runtime = 'host'` als gültiger Wert)
- Backend-Proxy: `backend/app/routers/host_agents.py` (`/api/v1/host-agents/{id}/terminal`)
- Disabled Container-Boss: `docker/docker-compose.agents.yml` (`mc-agent-boss` Block kommentiert)
- Plan: `docs/plans/2026-04-17-boss-host-migration.md`
- Verwandte ADRs: ADR-003 (Triple-Runtime, jetzt 4-Runtime), ADR-011 (HTTP-Polling), ADR-013 (Docker-V2)
- Memory-Leak-Kontext: `feedback_container_memory_leak.md`
