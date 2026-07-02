# ADR-006 — Jinja2-Template als Single Source of Truth für Agent-Config

**Status:** Accepted
**Datum:** 2025 (ursprünglich beim CLI-Bridge-Setup)
**Scope:** Backend/Provisioning

## Kontext

Jeder Agent hat mehrere Config-Dateien die auf verschiedenen Systemen existieren müssen:
- `SOUL.md` — System-Prompt (Persönlichkeit, Rolle, Regeln)
- `TOOLS.md` — Verfügbare API-Calls (gefiltert nach Scopes)
- `HEARTBEAT.md` — Status-Protokoll für Gateway
- `settings.json` — openclaude/claude Code Config (enabledPlugins, Model, systemPrompt)
- `agent.env` — Environment Variables (MC_AGENT_TOKEN, CLAUDE_CONFIG_DIR)
- `worker.sh` — Host-Worker-Loop (Legacy cli-bridge)
- `MEMORY.md` — Knowledge-Base-Template

Diese existieren auf dem Host (`~/.openclaw/agents/{slug}/`) UND im Docker-Container (gemountet). Bei jeder Änderung an Agent-Konfiguration (Scopes, Skills, Model, Plugins, Soul) müssen mehrere Dateien konsistent aktualisiert werden.

Ursprünglich hardcoded, manuell editiert — schnell inkonsistent geworden. Besonders problematisch: Scope-Änderungen mussten manuell in TOOLS.md reflektiert werden (sonst hatte der Agent Curl-Commands für Endpoints die er nicht nutzen durfte).

## Entscheidung

**Alle Agent-Config-Dateien werden aus Jinja2-Templates gerendert.** Die Templates sind die **Single Source of Truth**, die Ausgabe-Dateien sind Artefakte.

Templates liegen in `backend/templates/`:
- `SOUL.md.j2` — nutzt `role`, `agent_name`, `scopes`, `board_context`
- `HEARTBEAT.md.j2` — Status-Protokoll + Curl-Beispiele
- `USER.md.j2` — Persona des Operators (für Agents)
- `MEMORY.md.j2` — Memory-Struktur
- `cli_agent_settings.json.j2` — openclaude/claude Code settings
- `cli_agent.env.j2` — env vars
- `cli_agent_worker.sh.j2` — worker.sh Loop (Legacy Host)

**Rendering-Trigger:**
- `POST /agents` → `_provision_agent_background()` → alle Templates rendern
- `PATCH /agents/{id}/config` → `sync_agent_config_to_gateway()` rendert + syncht via RPC
- `POST /agents/{id}/provision` → volles Reprovisioning
- `POST /agents/{id}/sync-config` → ausgewählte Dateien re-rendern
- CLI-Bridge HTTP-Endpoint `/provision/{slug}` nutzt dieselben Templates

**DB = Input, Templates = Logik, Dateien = Output.** Direkte File-Edits werden beim nächsten Reprovision überschrieben (siehe CLAUDE.md Warnung).

## Alternativen

- **A: Config direkt in DB als JSON speichern** → verworfen weil:
  - Keine Git-Historie für Config-Änderungen
  - Keine Code-Review für Änderungen am Inhalt (Prompts, Curl-Commands)
  - Schwer testbar (SQL-Snapshots statt Text-Diffs)
- **B: Python-Strings mit f-Format** → verworfen weil:
  - Schlechte Lesbarkeit für 200+ Zeilen Markdown
  - Kein Syntax-Highlighting
  - Escaping-Hölle bei Curl-Beispielen
- **C: Statische Files mit Placeholders** → verworfen weil Scope-Filtering (TOOLS.md Sections conditional) nicht möglich
- **D: CLI-Templates manuell pflegen** → verworfen weil Drift zwischen Host und Gateway

## Konsequenzen

### Positiv
- **Versionierbarkeit**: Templates in Git, alle Änderungen nachvollziehbar
- **Konsistenz**: Eine Änderung → alle Agents beim nächsten Reprovision konsistent
- **Scope-Filtering**: `{% if 'tasks:write' in scopes %}...{% endif %}` → nur erlaubte Sektionen im Output
- **Testbar**: Template-Rendering unit-testbar ohne DB
- **Fallback**: Wenn Gateway-Sync fehlschlägt, lokale Templates als Backup
- **Lesbarkeit**: Markdown + Jinja2 gut lesbar, kann von Menschen reviewed werden

### Negativ
- **Indirektion**: "Wo ändere ich X?" — nicht direkt im File, sondern im Template
- **Reprovision-Pflicht**: Nach Template-Änderung muss jeder Agent neu provisioniert werden, sonst alte Version
- **Template-Drift**: Wenn Templates + DB-Fields auseinandergehen (neuer Field im Model, Template weiss nichts), Rendering bricht
- **Jinja2-Sandbox**: Keine Python-Logik im Template, kompliziertere Logik muss in Python vorher (Context-Prep)
- **Whitespace-Probleme**: Jinja2 `{%-` vs `{%` Semantik ist subtil, wurden schon Bugs produziert (siehe feedback_claude_binary_agent.md)

## Referenzen

- Templates: `backend/templates/*.j2`
- Renderer: `backend/app/services/template_renderer.py`
- CLI-Bridge nutzt: `scripts/cli-bridge.py:_provision_agent()` (lädt `backend/templates/`)
- Warnung: `CLAUDE.md` — "Immer Template bearbeiten, nie direkt DB-PATCH"
- Verwandt: ADR-009 (Scope-Separation), feedback_claude_binary_agent.md, feedback_soul_md_api_calls.md
