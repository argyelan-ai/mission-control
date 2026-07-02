"""
ToolsMdBuilder — Generiert TOOLS.md fuer Agents.

Aus agents.py extrahiert fuer bessere Testbarkeit und kuerzere Dateien.
Pure function (keine DB-Abhaengigkeit, reine String-Generierung).
"""


def generate_tools_md(
    name: str,
    emoji: str,
    raw_token: str,
    board_id: str | None,
    is_board_lead: bool = False,
    scopes: list[str] | None = None,
    runtime: str = "docker",
) -> str:
    """Generiert eine vorausgefuellte TOOLS.md fuer einen neu erstellten Agent.

    Wenn scopes angegeben, werden nur Sektionen fuer erlaubte Scopes generiert.
    scopes=None oder scopes=[] → alle Sektionen (backward compat).

    runtime: "docker" (cli-bridge, default) oder "host" (Boss). Beeinflusst
    nur die Vault-Sektion, weil host-Agents direkt aufs Filesystem ~/.mc/vault
    zugreifen statt auf den Container-Mount /vault.
    """
    from app.scopes import Scope

    def _has(scope: str) -> bool:
        """True wenn keine Scopes gesetzt (backward compat) oder Scope vorhanden."""
        if not scopes:
            return True
        return scope in scopes

    # ── Board-Sektionen ──────────────────────────────────────────────────
    board_section = ""
    if board_id:
        parts = []

        if _has(Scope.TASKS_READ):
            parts.append(f"""## Board-Snapshot lesen (alle Tasks + Memory)
GET $MC_API_URL/api/v1/agent/boards/{board_id}
Authorization: Bearer $MC_AGENT_TOKEN""")

            parts.append(f"""## Naechsten Task holen (Pull-Dispatch)
GET $MC_API_URL/api/v1/agent/boards/{board_id}/tasks/next
Authorization: Bearer $MC_AGENT_TOKEN

HTTP 200 → Task + Kontext zurueck (Task automatisch auf in_progress gesetzt)
HTTP 204 → Kein Task verfuegbar (Agent idle oder alle Dependencies blockiert)""")

            # Deliverables lesen
            parts.append(f"""## Deliverables eines Tasks lesen
```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{task_id}}/deliverables" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```""")

        if _has(Scope.TASKS_READ):
            parts.append(f"""## Board-Agents auflisten (fuer assigned_agent_id)

Wenn du Subtasks mit assigned_agent_id erstellen willst, hole die Agent-UUIDs via API:

```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/agents" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Antwort: Liste mit id, name, role, is_board_lead pro Agent.""")

        if _has(Scope.TASKS_CREATE):
            if is_board_lead:
                # Board Lead bekommt Projekt-Verwaltung + Orchestrator-Sektion
                parts.append(f"""## Projekte auflisten

BEVOR du ein neues Projekt erstellst, pruefe ob es schon existiert.
Projekte sind auch in der Board-Read Response enthalten (GET /agent/boards/{{board_id}} → "projects" Array).

```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/projects" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Antwort: Liste aller Projekte mit id, name, status, project_type, progress_pct, etc.

## Projekt erstellen (wenn der Task ein ganzes Projekt beschreibt)

Wenn der Task ein eigenstaendiges Projekt beschreibt (Website, App, Feature mit mehreren Teilen),
erstelle ZUERST ein Projekt und nutze dann dessen project_id bei den Subtasks.

Erkennungsmerkmale fuer Projekte:
- "Baue mir eine Website/App/Tool"
- Mehrere Komponenten (Frontend + Backend, Design + Implementierung)
- Eigenes Deployment oder eigenes Repository noetig

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/projects" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "name": "Projektname",
    "description": "Was gebaut werden soll",
    "project_type": "website",
    "priority": "medium"
  }}'
```

project_type: feature | website | content | research | automation | design | free
Antwort enthaelt die project_id — nutze sie in allen Subtasks.

## Subtask erstellen und delegieren (Orchestrator)

WICHTIG: Du bist Orchestrator. Erstelle IMMER Subtasks mit parent_task_id und assigned_agent_id.
Erstelle NIEMALS Tasks ohne diese Felder — sonst wird der Task dir selbst zugewiesen.

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "title": "Konkrete Aufgabe fuer den Agent",
    "description": "## Ziel\\nKonkret was erreicht werden soll.\\n\\n## Kontext\\n- Pfad: ~/Workspace/Projects/mission-control/\\n- URL: http://localhost\\n\\n## Guardrails\\n- Kein DB-Schema aendern\\n\\n## Erwarteter Output\\n- PR mit Aenderungen\\n\\n## Definition of Done\\n- Tests gruen",
    "credentials": "email: admin@mc.local / password: xxx",
    "parent_task_id": "DEINE-TASK-ID-HIER",
    "assigned_agent_id": "AGENT-UUID-HIER",
    "project_id": "PROJEKT-UUID-HIER-FALLS-VORHANDEN",
    "priority": "medium",
    "tags": ["backend", "api"]
  }}'
```

PFLICHT-Felder in der description (Agent kennt KEINEN Chat-Kontext!):
1. **Ziel** — Was genau soll erreicht werden?
2. **Kontext** — Pfade, URLs, Stack-Infos
3. **Guardrails** — Was NICHT gemacht werden soll
4. **Erwarteter Output** — Screenshots, PRs, Dateien
5. **Definition of Done** — Messbare Fertig-Kriterien

**Credentials** — Falls Login/API-Keys noetig: ins `credentials` Feld (NICHT in description!). Wenn unbekannt: den Operator fragen.

Felder:
- parent_task_id: Die Task-ID die DU erhalten hast (dein Haupt-Task)
- assigned_agent_id: UUID des Agents der den Subtask ausfuehren soll
- project_id: UUID des Projekts (falls du eins erstellt hast). Wird automatisch vom Parent geerbt wenn nicht gesetzt.
- depends_on: ["task-uuid", ...] — optionale Abhaengigkeiten (Subtask wartet auf diese Tasks)
- tags: Liste von Tag-Namen (optional). Werden in der Pipeline als farbige Labels angezeigt.

**delegation_type** (optional — aktiviert Contract-Check, 422 bei falschem Wert):
| Wert | Wann | Pflichtfelder |
|------|------|---------------|
| `code_change` | Agent soll Code aendern | `branch_name`, `acceptance_criteria` |
| `visual_proof` | Screenshots/visuelle Verifikation | `target_url`, `acceptance_criteria`, `expected_content` |
| `credential_bound` | Braucht Login/API-Key | `credentials`, `target_url`, `acceptance_criteria` |
| `review` | Agent reviewt einen anderen Task | `source_task_id` |
| `planning` | Planungs-Subtask | — |
Fuer Recherche-Tasks: delegation_type WEGLASSEN (kein Contract-Check). Ungueltige Werte (z.B. "research") fuehren zu 422.

**autonomy_level** (optional — steuert ob Operator-Freigabe noetig ist):
- `execute_low_risk` — sofort ausfuehren (Sandbox, kein Browser, keine Credentials, kein DB)
- Weglassen → konservativer Default → Operator-Freigabe noetig

WICHTIG: Wenn du einen Subtask mit parent_task_id erstellst, wird dein Parent-Task
automatisch auf in_progress gesetzt (= ACK). Du musst den Parent NICHT manuell bestaetigen.

Status: inbox | in_progress | review | done | blocked
Prioritaet: low | medium | high | critical""")
            else:
                parts.append(f"""## Task erstellen
POST $MC_API_URL/api/v1/agent/boards/{board_id}/tasks
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "title": "Task-Titel",
  "description": "MUSS Markdown sein. Mindest-Struktur:\n## Ziel\n...\n## Kontext\n...\n## Definition of Done\n...",
  "status": "inbox",
  "priority": "medium",
  "tags": ["bugfix"],
  "assigned_agent_id": "UUID-DES-ZIELAGENTS",
  "parent_task_id": "DEINE-TASK-ID-HIER"
}}

Felder:
- assigned_agent_id: UUID des Agents der den Task ausfuehren soll (PFLICHT beim Delegieren!)
- parent_task_id: Deine eigene Task-ID (PFLICHT beim Erstellen von Subtasks!)
- tags: Liste von Tag-Namen (optional). Beispiele: "backend", "frontend", "bugfix", "refactor"
- depends_on: ["task-uuid", ...] — optionale Abhaengigkeiten

**delegation_type** (optional — aktiviert Contract-Check, 422 bei falschem Wert):
| Wert | Wann | Pflichtfelder |
|------|------|---------------|
| `code_change` | Agent soll Code aendern | `branch_name`, `acceptance_criteria` |
| `visual_proof` | Screenshots/visuelle Verifikation | `target_url`, `acceptance_criteria`, `expected_content` |
| `credential_bound` | Braucht Login/API-Key | `credentials`, `target_url`, `acceptance_criteria` |
| `review` | Agent reviewt einen anderen Task | `source_task_id` |
| `planning` | Planungs-Subtask | — |
Fuer Recherche-Tasks: delegation_type WEGLASSEN. Ungueltige Werte (z.B. "research") fuehren zu 422!

**autonomy_level** (optional):
- `execute_low_risk` — sofort ausfuehren (Sandbox, kein Browser, keine Credentials, kein DB)
- Weglassen → Operator-Freigabe noetig

WICHTIG: Wenn du einen Subtask mit parent_task_id erstellst, wird dein Parent-Task
automatisch auf in_progress gesetzt (= ACK). Du musst den Parent NICHT manuell bestaetigen.

SKILLS/TOOLS PASSTHROUGH: Wenn der Haupt-Task auf ein bestimmtes Skill oder Tool verweist
(z.B. "nutze Stitch", "verwende /website", "nutze den FreeCode-Researcher"), dann MUSS
dieser Hinweis explizit in der description des Subtasks enthalten sein.
Der Ziel-Agent kennt keinen Chat-Kontext — nur was in der description steht.

Status: inbox | in_progress | review | done | blocked
Prioritaet: low | medium | high | critical""")

        if _has(Scope.TASKS_WRITE):
            parts.append(f"""## Task aktualisieren
**WICHTIG: Fuer Status-Aenderungen (ack/review/done/blocked/failed) IMMER die `mc`-CLI nutzen — nie raw PATCH.**
Der raw PATCH erfordert den `X-Dispatch-Attempt-Id` Header (Wert aus $X_DISPATCH_ATTEMPT_ID) — fehlt er, gibt der Server 409.
Die mc-CLI liest den Header automatisch aus /tmp/mc-context.env. Raw PATCH nur fuer Felder wie priority/title/project_id.

PATCH $MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{task_id}}
Authorization: Bearer $MC_AGENT_TOKEN
X-Dispatch-Attempt-Id: $X_DISPATCH_ATTEMPT_ID
Content-Type: application/json

{{
  "priority": "high",
  "project_id": "PROJEKT-UUID-ODER-NULL"
}}

Aenderbare Felder via raw PATCH: priority, title, description, project_id.
Status-Aenderungen: `mc ack` / `mc review` / `mc done` / `mc blocked` / `mc failed` nutzen.

Wenn du blockiert bist, nutze `mc blocked` (nicht raw PATCH):
`mc blocked --type missing_info "Was genau fehlt"`
`mc blocked --type technical_problem "Beschreibung"`

blocker_type: missing_info | technical_problem | decision_needed | permission_needed | dependency_blocked | other

## Kommentar zu Task hinzufuegen
POST $MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{task_id}}/comments
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Fortschrittsbericht oder Notiz zum Task",
  "comment_type": "message"
}}

comment_type: message | handoff | blocker | progress | resolution [terminal — auto-promotes Task zu review/done] | feedback""")

            # Deliverable registrieren
            parts.append(f"""## Deliverable registrieren (Ergebnis-Artefakt)
Registriere Ergebnisse als Deliverable — sichtbar im MC UI.

> **Hinweis:** `mc deliverable`, `mc pdf` und `mc telegram` loesen deine aktuelle Task automatisch auf.
> Du musst keine Task-ID angeben — das Backend findet sie via spawn_session_key.

```bash
# Einfachster Weg (bevorzugt):
mc deliverable --type document --title "Research-Ergebnis" --path /deliverables/$TASK_ID/report.md

# Oder via curl (Task-ID wird automatisch aufgeloest — kein board_id/task_id in der URL):
curl -s -X POST "$MC_API_URL/api/v1/agent/me/deliverable" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"deliverable_type": "document", "title": "Research-Ergebnis", "content": "# Titel\\n\\nVollstaendiger Markdown-Inhalt hier...", "path": "/deliverables/$TASK_ID/report.md"}}'
```

deliverable_type: `screenshot` | `file` | `url` | `artifact` | `document` | `data`
Pflichtfelder: `deliverable_type`, `title`.
**WICHTIG: `content` IMMER mitschicken** bei document/artifact/file — der `path` zeigt ins Container-Filesystem und ist vom Frontend nicht lesbar. Ohne `content` ist das Deliverable im UI leer.""")

            parts.append(f"""## Checkliste verwalten (Task-Fortschritt)

### Checkliste erstellen (als ALLERERSTES!)
```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{{{task_id}}}}/checklist" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"items": [{{"title": "Analyse", "sort_order": 0}}, {{"title": "Implementieren", "sort_order": 1}}, {{"title": "Tests", "sort_order": 2}}]}}'
```

### Item als done markieren
```
curl -s -X PATCH "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{{{task_id}}}}/checklist/{{{{item_id}}}}" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"status": "done"}}'
```

### Checkliste lesen (bei Recovery)
```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks/{{{{task_id}}}}/checklist" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

status-Werte: `pending` | `in_progress` | `done` | `blocked` | `skipped`""")

            # Crash-recovery / progress-tracking is now TaskChecklistItem
            # (Workstream A4, ADR-020). POST /checkpoint returns HTTP 410
            # and the TaskCheckpoint table is a read-only archive being
            # dropped in a follow-up migration. Agents use `mc checklist`
            # for progress and the reflection comment for lessons.
            parts.append(f"""## Fortschritt tracken (statt altem /checkpoint)
Checkliste + Progress-Kommentare ersetzen das alte /checkpoint Endpoint:

```bash
# Item hinzufuegen
mc checklist add "Schritt 1 — Models schreiben"
# Item als fertig markieren
mc checklist done <item_id>
# Liste zeigen
mc checklist list
# Progress-Kommentar (Update/Evidence/Next)
mc comment progress "Update — Models erstellt
Evidence — backend/app/models/foo.py:1-40, Tests gruen
Next — Endpoints verdrahten"
```

Bei Crash/Timeout/Re-Dispatch rendert der Recovery-Kontext deine Checkliste
mit `← HIER WEITERMACHEN` Marker am ersten offenen Item. Keine manuellen
Checkpoints mehr noetig.""")

        if _has(Scope.MEMORY_READ):
            parts.append("""## Memory-Suche (eigene + Team-Lessons retrievable)
Du kannst eigene frueheren Lessons und Team-Memory via CLI durchsuchen —
automatische Embeddings via Qdrant, Retrieval per Semantic-Search.

```bash
mc memory search "<query phrase>" --limit 5
```

Oder per HTTP direkt:
```
POST $MC_API_URL/api/v1/agent/memory/query
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{"query": "<search text>", "layers": ["semantic", "agent"], "top_k": 5}
```

Nutzt du das konsequent, findest du eigene Lessons wieder, lernst aus Team-
Erfahrungen, und vermeidest doppelte Arbeit.""")

        if _has(Scope.MEMORY_WRITE):
            parts.append(f"""## Board-Memory schreiben (board-scoped, sehen alle Agents auf dem Board)
POST $MC_API_URL/api/v1/agent/boards/{board_id}/memory
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Wichtige Erkenntnis oder Entscheidung",
  "title": "Optionaler Titel",
  "memory_type": "knowledge",
  "tags": ["tag1", "tag2"],
  "is_pinned": false
}}""")

        if _has(Scope.APPROVALS_CREATE):
            parts.append(f"""## Approval anfragen (Human-in-the-Loop — blockierende Aktion)
POST $MC_API_URL/api/v1/agent/boards/{board_id}/approvals
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "action_type": "deployment",
  "description": "Was genau genehmigt werden soll",
  "confidence": 0.85
}}""")

        if _has(Scope.CHAT_WRITE):
            chat_hint = ""
            if is_board_lead:
                chat_hint = """

HINWEIS: Board-Chat ist fuer direkte Konversation mit dem Operator.
NICHT fuer Task-bezogene Kommunikation. Dafuer → Task-Kommentare nutzen."""
            else:
                chat_hint = """

HINWEIS: Board-Chat ist fuer dringende Hilfe-Anfragen an Henry (Board Lead).
Nutze Board-Chat wenn du BLOCKIERT bist und schnelle Hilfe brauchst.
Fuer normale Task-Updates → Task-Kommentare nutzen."""
            parts.append(f"""## Chat-Nachricht an Board senden
POST $MC_API_URL/api/v1/agent/boards/{board_id}/chat
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Nachricht an den Board-Channel"
}}{chat_hint}""")

        # ── Projekt-Sektion ──────────────────────────────────────────
        if _has(Scope.PROJECT_READ):
            parts.append(f"""## Projekt-Kontext abrufen

WENN du einen Task mit project_id erhältst, lies zuerst das Projekt-Briefing:

```
curl -s "$MC_API_URL/api/v1/agent/projects/{{project_id}}" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Antwort enthält:
- briefing_doc (Markdown — immer zuerst lesen!)
- phases (Liste aller Phasen mit Status)
- last_active_phase_id (aktuelle Phase)

## Deliverables eines Projekts suchen

```
curl -s "$MC_API_URL/api/v1/agent/projects/{{project_id}}/deliverables?scope=phase&is_pinned=true" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Query-Parameter: scope=task|phase|project  is_pinned=true  tags=research,design""")

        if _has(Scope.PROJECT_WRITE):
            parts.append(f"""## Deliverable mit Projekt-Kontext registrieren (V2)

```bash
# Task-ID wird automatisch aufgeloest (kein board_id/task_id in der URL):
curl -s -X POST "$MC_API_URL/api/v1/agent/me/deliverable" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{{{
    "deliverable_type": "artifact",
    "title": "Competitor Analysis",
    "content": "Vollständiger Markdown-Inhalt des Deliverables",
    "scope": "phase",
    "tags": ["research", "analysis"],
    "is_pinned": false,
    "is_reusable": true,
    "git_commit": true
  }}}}'
```

scope: task | phase | project
is_pinned: true = in Agent-Kontext injiziert (sparsam!)
git_commit: true = in Phase-Branch committed (empfohlen bei scope=phase/project)
deliverable_type: `screenshot` | `file` | `url` | `artifact` | `document` | `data`

## Sub-Task erstellen

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/tasks" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{{{
    "title": "Recherche: OKLCH Color Spaces",
    "description": "## Ziel\\n...\\n## Kontext\\n...\\n## Definition of Done\\n...",
    "project_id": "{{project_id}}",
    "phase_id": "{{phase_id}}",
    "triggered_by_deliverable_id": "{{deliverable_id}}",
    "depends_on": ["{{current_task_id}}"]
  }}}}'
```

triggered_by_deliverable_id: IMMER setzen wenn aus Deliverable entstanden (Provenance!)

## Phase abschliessen

```
curl -s -X POST "$MC_API_URL/api/v1/agent/phases/{{phase_id}}/complete" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Erst aufrufen wenn ALLE Tasks der Phase `done` sind.""")

        if parts:
            board_section = "\n\n".join(parts) + "\n"

    # ── Board Lead Sektion ───────────────────────────────────────────────
    board_lead_section = ""
    if is_board_lead and _has(Scope.AGENTS_MANAGE):
        board_id_placeholder = board_id or "{board_id}"
        board_lead_section = f"""
---

## Neuen Agent erstellen (nur du als Board Lead)

Vorgehen:
1. Operator fragen: Template verwenden oder Agent von Grund auf einrichten?

2a. Template-Weg:
    GET $MC_API_URL/api/v1/agent/templates
    Authorization: Bearer $MC_AGENT_TOKEN
    → Template aus der Liste auswaehlen

    POST $MC_API_URL/api/v1/agent/templates/{{template_id}}/instantiate
    Authorization: Bearer $MC_AGENT_TOKEN
    Content-Type: application/json

    {{
      "board_id": "{board_id_placeholder}",
      "name": "Optionaler Name"
    }}

2b. Custom-Weg (ohne Template):
    POST $MC_API_URL/api/v1/agent/agents
    Authorization: Bearer $MC_AGENT_TOKEN
    Content-Type: application/json

    {{
      "name": "Agent-Name",
      "emoji": "🤖",
      "role": "Beschreibung der Rolle",
      "model": null,
      "skills": [],
      "board_id": "{board_id_placeholder}"
    }}

Antwort beider Endpoints: {{ "agent": {{...}}, "token": "..." }}
→ Agent wird automatisch provisioniert (einsatzbereit in ~10 Sekunden)
→ Token NUR EINMAL sichtbar — sofort an den Operator weitergeben!

Fortschritt erscheint im Activity-Feed.

## Eigenes SOUL.md lesen und aendern

Lesen:
```
curl -s "$MC_API_URL/api/v1/agent/config/soul_md" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Aendern (VORSICHT — aendert dein eigenes Verhalten!):
```
curl -s -X PUT "$MC_API_URL/api/v1/agent/config/soul_md" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"content": "Neuer SOUL.md Inhalt...", "reason": "Kurze Begruendung der Aenderung"}}'
```

**Regeln:**
- `reason` IMMER angeben — der Operator sieht die Aenderung im Activity-Feed
- Nur Fehler korrigieren oder fehlende Regeln ergaenzen — keine Grundstruktur aendern
- Im Zweifel den Operator fragen bevor du dein SOUL aenderst
- Aenderung wird automatisch zum Gateway/Disk synchronisiert
"""

    # ── Vertical-Sektionen (z.B. News-Studio Content-Pipeline) ───────────
    # Verticals registrieren (scope, builder) in app.verticals.hooks —
    # gestrippter Public-Release: leere Liste, keine Sektion.
    from app.verticals import hooks as vertical_hooks

    content_section = ""
    for _scope_str, _builder in vertical_hooks.tools_md_sections:
        if _has(Scope(_scope_str)):
            content_section += _builder({})

    # ── Credentials Sektion ──────────────────────────────────────────────
    credentials_section = ""
    if board_id and _has(Scope.CREDENTIALS_READ):
        credentials_section = f"""
---

## Credentials Vault (vs. System-Secrets) — ADR-033

MC hat **zwei** getrennte Geheimnis-Stores. Du nutzt nur einen davon direkt.

|  | `secrets` (System Token Wallet) | `credentials` (Task Vault) — **dein Store** |
|---|---|---|
| **Was** | 1 Eintrag pro Provider/Dienst | N Eintraege pro Use-Case, typed login/token/custom |
| **Beispiele** | openai_api_key, anthropic_api_key, github_token, discord_bot_token, xai_api_key, livekit_api_key | client-login, twitter-bearer, externer-API-Token, Trading-Account |
| **Wer schreibt** | Nur der Operator (Admin) | Jeder eingeloggte User |
| **Agent-Zugriff** | **Keiner** — Backend-Services nutzen sie im Namen des Operators | Lesen via API (siehe unten) |

**Faustregel:**
- LLM-Provider / GitHub / Discord / OpenClaw-Token → `secrets`. Backend nutzt sie. Frag nie danach, suche sie nie.
- Login/Token fuer eine task-spezifische Aktion (Website, externe API, Trading) → `credentials`. Du holst sie selbst aus dem Vault.

Wenn ein Dispatch-Brief eine `credential_id` (UUID) referenziert: hol sie via Vault-API (unten). Wenn du in einem Brief plotzlich `openai_api_key` o.ae. siehst: das ist ein System-Secret — den Operator gegenfragen statt selber nach API zu suchen.

### Alle Credentials auflisten (maskiert)
```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/credentials" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Antwort: Liste mit id, name, credential_type, url, notes, data_masked (Passwort/Token teilweise verdeckt).
Nutze diesen Endpoint um die richtige Credential-ID zu finden.

### Einzelne Credential holen (vollstaendig entschluesselt)
```
curl -s "$MC_API_URL/api/v1/agent/boards/{board_id}/credentials/{{credential_id}}" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Antwort: id, name, credential_type, url, notes, data (vollstaendiges entschluesseltes Dict).

**Wann nutzen:**
- Dispatch-Brief referenziert eine `credential_id` (UUID) — primaerer Weg
- Task erwaehnt eine externe Seite/Service bei der du dich einloggen musst
- Dispatch-Kontext hat kein Inline-`credentials`-Feld aber du brauchst einen Login

**Sicherheitshinweis:** Credentials nie in Kommentaren, Commits oder Logs speichern.
"""

    # ── Deploy Sektion ─────────────────────────────────────────────────
    deploy_section = ""
    if _has(Scope.DEPLOY_EXECUTE):
        deploy_section = f"""
---

## Docker-Umgebung deployen

Du fuehrst Docker-Befehle DIREKT im Terminal aus (nicht via API).
Erlaubte Services: backend, frontend, caddy.
NIEMALS: db, redis.

### Shell-Befehle (direkt ausfuehren)

Restart (schnell, kein Rebuild):
  cd ~/Workspace/Projects/mission-control && docker compose restart backend

Rebuild (nach Code-Aenderungen):
  cd ~/Workspace/Projects/mission-control && docker compose up --build -d backend

Backup vor groesseren Deployments:
  cd ~/Workspace/Projects/mission-control && ./backup.sh

Logs pruefen:
  docker compose logs backend --tail=50

### Monitoring-API (MC Backend)

Health-Check aller Services:
GET $MC_API_URL/api/v1/agent/deploy/services
Authorization: Bearer $MC_AGENT_TOKEN

Health-Check einzelner Service:
GET $MC_API_URL/api/v1/agent/deploy/services/{{service_name}}/health
Authorization: Bearer $MC_AGENT_TOKEN

Deploy-History abrufen:
GET $MC_API_URL/api/v1/agent/deploy/history
Authorization: Bearer $MC_AGENT_TOKEN

### Deploy aufzeichnen (NACH jedem Deploy)
POST $MC_API_URL/api/v1/agent/deploy/record
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "service": "backend",
  "action": "rebuild",
  "success": true,
  "health_status": "healthy",
  "duration_seconds": 45.2
}}

action: rebuild | restart | rollback | backup
service: backend | frontend | caddy

### Workflow
1. Backup erstellen (bei Rebuild)
2. Docker-Befehl ausfuehren
3. 30 Sekunden warten
4. Health-Check via API
5. Deploy aufzeichnen via API
6. Bei Fehler: Rollback (docker compose restart) + aufzeichnen mit rolled_back=true

### ABSOLUTE GRENZEN
- KEIN docker compose down ohne Approval des Operators
- KEINE Aenderungen an .env
- db und redis NIEMALS anfassen

---

## Externe App-Deployments

### Schritt 0: Credentials holen
GET $MC_API_URL/api/v1/agent/deploy/credentials
Authorization: Bearer $MC_AGENT_TOKEN

Speichere die Werte als Variablen: VERCEL_TOKEN, CF_TOKEN, CF_ZONE_ID, SB_TOKEN

### Schritt 1: Vercel CLI installieren (einmalig)
```bash
npm install -g vercel
```

### Schritt 2: Frontend zu Vercel deployen
```bash
cd /pfad/zum/projekt
vercel deploy --prod --token=$VERCEL_TOKEN --yes
```
Rueckgabe enthaelt die Deployment-URL (z.B. https://projekt-abc.vercel.app)

### Schritt 3: Cloudflare-Subdomain erstellen
```bash
curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \\
  -H "Authorization: Bearer $CF_TOKEN" \\
  -H "Content-Type: application/json" \\
  --data '{{"type":"CNAME","name":"APP_NAME","content":"cname.vercel-dns.com","proxied":true}}'
```
Ersetze APP_NAME mit dem gewuenschten Subdomain-Namen (z.B. "shop" → shop.your-domain.com)

### Schritt 4: Domain ins Vercel-Projekt eintragen
```bash
vercel domains add APP_NAME.your-domain.com --token=$VERCEL_TOKEN
```

### Schritt 5: Supabase-Projekt erstellen (optional, wenn App DB braucht)
```bash
npx supabase projects create APP_NAME --org-id ORG_ID --db-password PASS --token $SB_TOKEN
```

### Schritt 6: Security-Check
Nach JEDEM externen Deploy diese Checks ausfuehren:

```bash
# HTTPS-Redirect pruefen
curl -sI http://APP_NAME.your-domain.com | grep -i "location"

# Security-Headers pruefen
curl -sI https://APP_NAME.your-domain.com

# Sensitive Pfade testen (muessen 404 sein)
curl -s -o /dev/null -w "%{{http_code}}" https://APP_NAME.your-domain.com/.env
curl -s -o /dev/null -w "%{{http_code}}" https://APP_NAME.your-domain.com/.git/config
```

Checklist:
- strict-transport-security (HSTS) — MUSS vorhanden sein
- x-content-type-options: nosniff — SOLL vorhanden sein
- x-frame-options — SOLL vorhanden sein
- .env und .git NICHT erreichbar (404)
- Keine Secrets im HTML (curl -s URL | grep -iE "api.key|token|secret")

### Schritt 7: Optische Pruefung + Screenshot an den Operator
```bash
# Browser oeffnen und Screenshot machen (dev-browser)
dev-browser <<'EOF'
const page = await browser.getPage("deploy");
await page.goto("https://APP_NAME.your-domain.com");
await page.waitForLoadState("networkidle");
const buf = await page.screenshot({{ fullPage: true }});
const path = await saveScreenshot(buf, "deploy-check.png");
console.log(path);
EOF
# Pfad aus Output ablesen (~/.dev-browser/tmp/deploy-check.png)

# Screenshot an den Operator via Telegram senden (via mc verify fuer URLs oder mc deliverable+telegram)
# Fuer Live-URLs: Visual Verification-Service macht Screenshot + Metriken + postet
mc verify https://APP_NAME.your-domain.com --caption "Deploy-Check: APP_NAME.your-domain.com — [OK/Probleme]"

# Oder fuer lokalen Screenshot: erst als Deliverable (type=screenshot) registrieren,
# dann mit --photo an Telegram anhaengen
mc deliverable --type screenshot --title "Deploy-Check" --path "~/.dev-browser/tmp/deploy-check.png"
mc telegram "Deploy-Check: APP_NAME.your-domain.com — [OK/Probleme]" --photo <deliverable-id>
```

### Workflow-Zusammenfassung
1. Credentials holen (GET /api/v1/agent/deploy/credentials)
2. vercel deploy → Deployment-URL
3. Cloudflare DNS → Subdomain erstellen
4. vercel domains add → Domain verknuepfen
5. Security-Check (HTTPS, Headers, Sensitive Pfade)
6. Optische Pruefung (Screenshot + Vision-Analyse)
7. Screenshot + Bericht an den Operator via Telegram
8. Deploy aufzeichnen (POST /api/v1/agent/deploy/record)
"""

    # ── Install-Request Sektion ──────────────────────────────────────────
    install_request_section = ""
    if _has(Scope.AGENTS_MANAGE):
        install_request_section = f"""
---

## Plugin-Management für Worker

Du darfst bereits installierte CLI-Plugins an Worker-Agents (deselben Boards)
zuweisen oder entfernen — ohne Operator-Approval. Use-Case: Davinci braucht
`higgsfield-mcp`, Sparky soll nur `superpowers` bekommen, Tester braucht
kein Plugin-Overhead etc.

**Vorgehen:**
1. Liste zeigen was verfügbar ist (`mc plugin-list` / `GET /plugins`)
2. Optional: pruefen was der Worker heute hat (`mc plugin-show <agent>`)
3. Neue Allowlist setzen (`mc plugin-assign <agent> [...]`)
4. Optional: Worker-Restart damit Plugins sofort aktiv (`--restart` oder `mc worker-restart <agent>`)

Wenn das gewünschte Plugin NICHT im shared cache ist → neue Installation
via `Installation Requests` (Sektion unten) anfordern. Operator-Approval
pflicht für Supply-Chain-Schutz.

### Quick-Form via mc CLI (empfohlen)

    mc plugin-list                                                  # shared cache
    mc plugin-show Davinci                                          # Davincis Allowlist
    mc plugin-assign Davinci higgsfield-mcp@anthropic-agent-skills --restart
    mc plugin-unassign Davinci superpowers@claude-plugins-official
    mc worker-restart Davinci                                       # falls ohne --restart gesetzt

Agent-Name ist case-insensitive und wird im aktuellen Board resolved.
Fuer alle Befehle gibt es auch die raw-curl-Form unten.

### Verfügbare Plugins auflisten

    curl http://mc-backend:8000/api/v1/agent/plugins \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN"

Response: `{{"plugins": [{{"key": "...", "name": "...", "source": "...", "version": "..."}}, ...], "total": N}}`

Der `key` (z.B. `frontend-design@claude-plugins-official`) ist was du für
Zuweisung brauchst.

### Aktuelle Plugin-Zuweisung eines Workers lesen

    curl http://mc-backend:8000/api/v1/agent/agents/<target-agent-id>/plugins \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN"

Response: `{{"agent_id": "...", "agent_name": "Davinci", "cli_plugins": [...] oder null}}`
- `null` = alle installierten Plugins (default)
- `[]` = keine Plugins
- `[...]` = explizite Allowlist

### Plugin-Zuweisung setzen

    curl -X PATCH http://mc-backend:8000/api/v1/agent/agents/<target-agent-id>/plugins \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "cli_plugins": ["superpowers@claude-plugins-official", "higgsfield-mcp@anthropic-agent-skills"],
        "restart_worker": true
      }}'

**cli_plugins semantik**:
- `null` (JSON: `null`) → Worker bekommt alle installierten Plugins
- `[]` (leere Liste) → Worker bekommt NICHTS
- `["a", "b"]` → nur diese (Allowlist)

**Additive Zuweisung**: Backend setzt das Feld komplett neu, keine Merge-Logik.
Wenn du EIN Plugin ergaenzen willst, erst GET → Liste kopieren → ergaenzen → PATCH.

**restart_worker** (default false):
- `false` → neue Plugins sind erst nach manuellem Worker-Restart oder naechstem
  Container-Restart aktiv. Laufender Task-Kontext bleibt erhalten.
- `true` → Worker-Session (claude in tmux Window 0) wird gekillt + neu gestartet.
  Neue Plugins sofort aktiv, aber laufender Task-Kontext ist WEG.
  Nur fuer CLI-Bridge-Agents — host-Runtime (Boss selbst) hat keinen Worker.

**Faustregel:** Wenn du einen Worker neu konfigurierst der gerade NICHTS tut
(current_task_id=null, idle) → `restart_worker: true` damit Plugins sofort live sind.
Wenn der Worker an einem Task arbeitet → `false`, restarten sobald der Task fertig ist.

### Worker-Session manuell neu starten

Wenn du Plugins ohne `restart_worker` gesetzt hast, oder der Worker aus anderen
Gruenden neu laden soll:

    curl -X POST http://mc-backend:8000/api/v1/agent/agents/<target-agent-id>/worker/restart \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN"

WARNUNG: laufender Task-Kontext geht verloren. Pruefe vorher via
`GET /agent/agents/<id>/detail` dass `current_task_id` null ist.

### Guards
- Nur Board Leads dürfen Plugins zuweisen + Worker restarten (du bist einer)
- Target muss zum selben Board gehoeren
- Board-Leads koennen einander keine Plugins/Restarts setzen (nur self)
- DB ist Source of Truth, Disk-Sync läuft automatisch (settings.json + plugins/cache)
- Worker-Restart nur fuer `agent_runtime=cli-bridge` — Boss (host-runtime) nicht betroffen

---

## Installation Requests

Stelle Install- oder Uninstall-Requests für **Skills, Plugins und MCP-Server**
die NOCH NICHT im shared cache sind. Der Operator approved oder rejected in
seiner Inbox. Erfolgreicher Approval triggert automatische Installation via
InstallExecutor (inkl. Rollback bei Smoke-Test-Fehler bei MCP).

**Wichtig:** Für MCP-Installationen bei anderen Agents NICHT manuell pip install /
Container-Edits vornehmen — immer dieses System verwenden.

Endpoint: POST /api/v1/agent/install-requests

### Callback-Koppelung (wichtig)

Stelle den Request mit `"task_id": "<dein-aktueller-task-uuid>"` — nach
erfolgreichem Install postet Backend automatisch einen `install_completed`
Comment auf diese Task. Mirror zum `subtask_completed` Pattern:
naechster Poll-Cycle → du siehst den Callback im Task-Kontext, weisst
dass das Item live ist und kannst mit der naechsten Aktion fortfahren.

Ohne `task_id` → nur ein `install.*` activity_event, kein Auto-Comment.
Du muesstest aktiv `GET /approvals` pollen oder im Agent-Feed nachschauen.

**Wichtig — waehrend du wartest**: bleib `in_progress`, NICHT `blocked`. Der
Callback kommt automatisch per Poll; kein Mensch muss eingreifen. `blocked`
waere falsch und der Operator koennte den Blocker ohnehin nicht resolven
(niemand weiss was zu tun ist, der Callback regelt es ja selbst).

### Was nach success passiert — kein zusaetzlicher assign-Call noetig

Der InstallExecutor traegt das installierte Item nach Erfolg AUTOMATISCH in
das passende Agent-Feld ein:

- **install_skill** → Name wird in `target_agent.cli_skills` appended
- **install_plugin** → Name wird in `target_agent.cli_plugins` appended
- **install_mcp** → Name wird in `target_agent.mcp_servers` appended + MCP-Smoke-Test

Danach triggert der Executor `sync_config` damit die Aenderung bei CLI-Bridge
Agents in claude-config landet. Du musst NICHT zusaetzlich `mc plugin-assign`
oder `mc worker-restart` aufrufen — der Install-Flow ist komplett autonom.

`mc plugin-assign` ist NUR fuer das Zuweisen von BEREITS-installierten
Plugins an andere Worker gedacht (wenn der Shared Cache das Plugin schon
hat und du es einem zweiten Worker geben willst). Skills haben keinen
separaten CLI-Command — sie sind nach Install-Success automatisch zugewiesen.

### Skill install

    curl -X POST http://mc-backend:8000/api/v1/agent/install-requests \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "type": "skill",
        "operation": "install",
        "source": "github:anthropic/skill-web-performance",
        "name": "web-performance",
        "target_agent_id": "<target-uuid>",
        "reason": "Agent failed 3 perf-debug tasks — dieser Skill hat Checklisten dafür",
        "autonomy_level": "L2",
        "task_id": "'"$TASK_ID"'"
      }}'

### MCP install

    curl -X POST http://mc-backend:8000/api/v1/agent/install-requests \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "type": "mcp",
        "operation": "install",
        "source": "github:geopopos/higgsfield_ai_mcp",
        "name": "higgsfield-ai",
        "target_agent_id": "<davinci-uuid>",
        "reason": "Davinci braucht MCP-Tools fuer Higgsfield Image/Video-Generierung im Marketing-Projekt",
        "autonomy_level": "L2"
      }}'

Nach Approval installiert der InstallExecutor das Paket, legt das Manifest
unter ~/.mc/mcp-servers/<name>/ an und synct Davincis .mcp.json.
Smoke-Test scheitert → automatischer Rollback.

Response: 201 mit approval_id + existing=false (oder 200 + existing=true bei Duplikat).

### Uninstall

    curl -X POST http://mc-backend:8000/api/v1/agent/install-requests \\
      -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "type": "mcp",
        "operation": "uninstall",
        "name": "higgsfield-ai",
        "target_agent_id": "<target-uuid>",
        "reason": "nicht mehr benötigt"
      }}'

### Allowlist-Quellen
- **skill**: github:anthropic/*, github:obra/*, github:getcursor/*, ~/.mc/skills/*
- **plugin**: claude-plugins-official, github:claude-plugins/*, github:anthropic/*
- **mcp**:
  - npm:@modelcontextprotocol/server-*
  - npm:@supabase/*, npm:@vercel/*, npm:@cloudflare/mcp-*
  - github:<any-org>/<repo-mit-mcp-im-namen> (z.B. `geopopos/higgsfield_ai_mcp`)

### Wichtig
- Schreibe einen **konkreten Grund**: welche Task scheiterte, warum DIESER Skill/Plugin/MCP,
  gibt es eine Alternative? Der Operator approved schneller wenn Kontext klar ist.
- Duplikate werden automatisch erkannt — gleicher Request 2× → selbe approval_id.
- Bereits-installiert-Check: HTTP 409 wenn Agent das Item schon hat.
- Requests expiren nach 7 Tagen.
"""

    # ── Knowledge-Sektionen ──────────────────────────────────────────────
    knowledge_parts = []
    if _has(Scope.KNOWLEDGE_READ):
        knowledge_parts.append(f"""## Knowledge Base lesen
GET $MC_API_URL/api/v1/agent/knowledge
Authorization: Bearer $MC_AGENT_TOKEN

Optionale Parameter:
  ?memory_type=knowledge|lesson|reference|research|journal|weekly_review|insight
  ?search=suchbegriff
  ?limit=50

Gibt alle relevanten Eintraege zurueck: eigene Knowledge, Board-Memory, globale Knowledge.""")

    if _has(Scope.KNOWLEDGE_WRITE):
        knowledge_parts.append(f"""## Eigenen Knowledge-Eintrag schreiben
POST $MC_API_URL/api/v1/agent/knowledge
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "Inhalt des Eintrags",
  "title": "Optionaler Titel",
  "memory_type": "knowledge",
  "tags": ["tag1"],
  "scope": "agent"
}}

scope: "agent" (nur ich sehe es) | "board" (alle Board-Agents) | "global" (alle)
memory_type: knowledge | lesson | reference | research | journal | weekly_review | insight""")

    knowledge_section = "\n\n".join(knowledge_parts)

    # ── Vault Sektion (Karpathy Wiki) ────────────────────────────────────
    vault_section = ""
    if _has(Scope.VAULT_WRITE):
        if runtime == "host":
            _vault_location = (
                f"**Dein Ordner:** `$AGENT_VAULT_PATH` "
                f"(host-Pfad `~/.mc/vault/agents/{name.lower()}/`)"
            )
        else:
            _vault_location = (
                f"**Dein Ordner:** `$AGENT_VAULT_PATH` "
                f"(gemappt auf `/vault/agents/{name.lower()}/` im Container)"
            )
        vault_section = f"""## Vault — Langzeit-Gedaechtnis (Karpathy Wiki)

Mission Controls kollektives Gedaechtnis lebt in einem Markdown-Vault unter `~/.mc/vault/`.
Du kannst direkt ins Filesystem schreiben UND via Backend-API.

{_vault_location}
**Shared Inbox:** `$AGENT_VAULT_INBOX` (fuer agentenuebergreifende Schreibvorgaenge)

### Eigene Lessons schreiben (direktes Filesystem)

```bash
cat > $AGENT_VAULT_PATH/lessons/$(date +%Y-%m-%d)-rate-limit-xai.md <<'EOF'
---
id: $(uuidgen)
type: lesson
agent: {name.lower()}
date: $(date -Iseconds)
tags: [api, rate-limiting]
---
# Rate Limit on xAI API

**Context:** Task #1234
**Observation:** xAI returns 429 above 10 req/s
**Lesson:** Add exponential backoff with base=2, max_delay=60s
EOF
```

**Wichtig:** Jede Datei MUSS `id`, `type`, `agent`, `date` im Frontmatter haben.
Der Watcher verschiebt ungueltige Dateien nach `_rejected/`.

### Agentenuebergreifende Entscheidungen (via Backend-API)

Fuer Dateien die andere Agents betreffen (z.B. `global/decisions/...`), nutze die Inbox-API.
Das Backend-Compactor mergt dein Envelope in den kanonischen Pfad.

**write_note Schema (PFLICHT-FELDER):**

```json
{{
  "title": "5-7 Wort lesbarer Titel",
  "content": "Markdown body mit [[wikilink]]s inline",
  "type": "knowledge | lesson | reference | journal | note",
  "tags": ["tag1", "tag2"],
  "related_notes": ["[[note-slug-1]]", "[[note-slug-2]]"],
  "relations": {{"note-slug-1": "supersedes"}}
}}
```

`related_notes` ist optional, aber **empfohlen**: erst `search_notes()`, dann
2-4 thematisch passende Treffer verlinken (auch inline in `content`). Leere
Liste ist erlaubt — der nächtliche Wikilink-Backfill verknüpft orphan-Notes
nachträglich automatisch über Qdrant-Ähnlichkeit + Spark-LLM.
Erlaubte Relation-Types: `supersedes | contradicts | refines | example-of | depends-on | related-to`

```bash
# Zuerst suchen, dann verlinken
curl "$MC_API_URL/api/v1/agent/vault/search?q=auth+migration" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"

# Dann Note schreiben (related_notes optional aber empfohlen)
curl -X POST "$MC_API_URL/api/v1/agent/vault/note" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "title": "Auth Migration Learnings",
    "content": "Bei OAuth2-Migration: Refresh-Tokens NICHT in localStorage. Siehe auch [[jwt-auth-overview]] und [[security-baseline]].",
    "type": "lesson",
    "target": "global/lessons/auth-migration.md",
    "tags": ["auth", "security"],
    "related_notes": ["[[jwt-auth-overview]]", "[[security-baseline]]"],
    "relations": {{"jwt-auth-overview": "supersedes"}},
    "task_id": "$TASK_ID",
    "idempotency_key": "lesson-auth-migration"
  }}'
```

`idempotency_key` verhindert Duplikate bei Timeout + Retry. `task_id`
ist optional aber **empfohlen wenn du gerade an einem Task arbeitest** —
es verlinkt deine Note mit allen anderen Notes + Deliverable-Wrappers
desselben Tasks (siehe "Task-Klammer" unten).

### Vault durchsuchen

```bash
# Volltext-Suche ueber alle Notes + Deliverable-Wrappers + extrahierte PDF-Texte
curl "$MC_API_URL/api/v1/agent/vault/search?q=rate+limit" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"

# Filter nach type=deliverable (nur Wrappers fuer Files/Screenshots/Docs)
curl "$MC_API_URL/api/v1/agent/vault/search?q=wetter&type=deliverable" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"

# Einzelne Note lesen
curl "$MC_API_URL/api/v1/agent/vault/note/agents/{name.lower()}/lessons/2026-05-14-rate-limit-xai.md" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

### Vault Files — Deliverables als Wrapper + Anhang

Jedes Task-Deliverable hat im Vault einen Markdown-Wrapper unter
`/vault/agents/<slug>/deliverables/*.md`. Der Wrapper enthaelt
Frontmatter (`task`, `deliverable_id`, `attachment_path`, `attachment_mime`)
und embedded den echten File aus `/vault/attachments/`. Such-Hits mit
`type:"deliverable"` zeigen genau auf diese Wrappers.

```bash
# Wrapper-Markdown lesen (z.B. nach Vault-Search hit)
Read /vault/agents/researcher/deliverables/wetter-staufen-2026-05-15.md

# PDF nativ lesen (mit pages-Parameter fuer lange Dokumente)
Read /vault/attachments/files/<deliverable-id>.pdf  (pages: "1-5")

# Bild nativ lesen — wird als Vision-Input interpretiert, du SIEHST das Bild
Read /vault/attachments/images/<deliverable-id>.png
```

**Bei PDF-Wrappers steht extrahierter Text unter `## Auto-extracted`** —
oft reicht das Wrapper-Markdown, ohne dass du das PDF selbst oeffnen musst.

**Binary-Files in `/vault/attachments/` NIE in-place editieren.** Wenn du
eine neue Version eines PDF/Bilds brauchst:
1. Neuen Wrapper anlegen (`<topic>-v2.md`) — der bestehende Deliverable-Flow
   (`mc deliverable --type ...` oder PATCH /api/v1/agent/me/deliverable)
   erzeugt den Wrapper automatisch
2. Im Frontmatter `supersedes: [[<alter-wrapper-id>]]`
3. Body-Sektion `## Vorgaenger` mit Wikilink zur alten Version

### Task-Klammer — verwandte Notes + Files

Wenn du Wrapper, Lessons und Memorys mit derselben `task_id` schreibst,
kannst du sie spaeter alle gemeinsam finden:

```bash
# Alle Notes + Wrappers + Lessons eines Tasks
curl "$MC_API_URL/api/v1/agent/vault/related/$TASK_ID" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN"
```

Use-Case: Du oeffnest einen alten Wetter-Report-Wrapper via Search —
mit related findest du sofort die Recherche-Lessons und Memorys, die
der vorherige Agent waehrend dieses Tasks geschrieben hat.

### Ordner-Disziplin

- Eigene Lessons/Notes → `$AGENT_VAULT_PATH/lessons/` oder `notes/`
- NIEMALS direkt in den Ordner eines anderen Agents schreiben — der Watcher lehnt Pfad-Eigentuemerschafts-Verletzungen ab
- Shared Knowledge (Entscheidungen, Projekt-Notes) → immer Inbox-API mit explizitem `target`"""

    # ── Memory Sektion ────────────────────────────────────────────────────
    memory_section = ""
    if _has(Scope.MEMORY_WRITE):
        memory_section = f"""## Eigene Memory aktualisieren (persistiert zwischen Sessions)
PATCH $MC_API_URL/api/v1/agent/me/memory
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "content": "# {name} Memory\\n\\n## Gelerntes\\n- ...\\n\\n## Konventionen\\n- ..."
}}

Achtung: Vollstaendiger Inhalt — nicht append. GET /api/v1/agent/me/memory zum Lesen."""

    # ── mc remember (vault shortcut) ────────────────────────────────────
    vault_remember_section = ""
    if _has("vault:write"):
        vault_remember_section = """## mc remember — Schnell etwas merken

```bash
mc remember "Was du gelernt hast"
mc remember "Titel" --content "Body" --type knowledge
mc remember "Lesson" --tags "docker,restart" --type lesson
```

Shortcut fuer `mc vault-write`. Defaults: type=lesson,
auto-Title aus Text, auto-Idempotency-Key, $TASK_ID aus env."""

    # ── Heartbeat Sektion ────────────────────────────────────────────────
    heartbeat_section = ""
    if _has(Scope.HEARTBEAT):
        heartbeat_section = f"""## Eigenen Status melden
POST $MC_API_URL/api/v1/agent/heartbeat
Authorization: Bearer $MC_AGENT_TOKEN
Content-Type: application/json

{{
  "status": "busy",
  "context_tokens": 45000
}}

Status: online | busy | idle | offline"""

    # ── Help Request Sektion ─────────────────────────────────────────────
    help_request_section = ""
    if _has(Scope.TASKS_HELP):
        help_request_section = f"""## Help Request — Andere Agents um Hilfe bitten

Wenn du fuer deine Aufgabe Unterstuetzung brauchst die ausserhalb deiner
Kompetenz liegt, kannst du einen Help Request stellen. Dein Task wird
automatisch pausiert bis das Ergebnis da ist.

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/help-request" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "needed_role": "researcher",
    "title": "Kurze Beschreibung was du brauchst",
    "context": "Detaillierter Kontext: was genau, wofuer, welches Format"
  }}'
```

Verfuegbare Rollen: researcher, developer, writer, reviewer, deployer, planner, tester.
Du bekommst das Ergebnis als Nachricht und machst dann weiter.
WICHTIG: Nutze Help Requests nur wenn du wirklich nicht weiterkommst.
Versuche zuerst selbst, bevor du andere Agents einbeziehst.

## Klaerungsfrage stellen — den Operator direkt fragen

Wenn du eine Entscheidung oder Klaerung vom Operator brauchst, stelle eine
strukturierte Frage. Dein Task wird pausiert bis der Operator antwortet.

```
curl -s -X POST "$MC_API_URL/api/v1/agent/boards/{board_id}/clarification" \\
  -H "Authorization: Bearer $MC_AGENT_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "question": "Deine konkrete Frage an den Operator",
    "options": ["Option A", "Option B"]
  }}'
```

Das `options`-Feld ist optional. Nutze es wenn du Antwort-Vorschlaege hast.
Du bekommst die Antwort des Operators als Nachricht und machst dann weiter."""

    # ── Browser-Referenz (fuer alle Agents) ────────────────────────────
    browser_section = """
---

## Browser-Referenz

### dev-browser (Primary — Playwright-basiert, sandboxed)

```bash
# URL oeffnen + Status pruefen
dev-browser <<'EOF'
const page = await browser.getPage("main");
await page.goto("URL");
console.log(JSON.stringify({ url: page.url(), title: await page.title() }));
EOF

# Seite analysieren (Element-Discovery)
dev-browser <<'EOF'
const page = await browser.getPage("main");
const result = await page.snapshotForAI();
console.log(result.full);
EOF

# Element klicken
dev-browser <<'EOF'
const page = await browser.getPage("main");
await page.getByRole("button", { name: "Submit" }).click();
EOF

# Feld ausfuellen
dev-browser <<'EOF'
const page = await browser.getPage("main");
await page.fill("#email", "user@example.com");
EOF

# Viewport-Screenshot
dev-browser <<'EOF'
const page = await browser.getPage("main");
const buf = await page.screenshot();
const path = await saveScreenshot(buf, "screenshot.png");
console.log(path);
EOF

# Ganzseitiger Screenshot
dev-browser <<'EOF'
const page = await browser.getPage("main");
const buf = await page.screenshot({{ fullPage: true }});
const path = await saveScreenshot(buf, "full-page.png");
console.log(path);
EOF
```

Screenshots werden nach `~/.dev-browser/tmp/` gespeichert. Pfad kommt aus `console.log(path)`.

### Persistenter Browser fuer externe Seiten (Login-Sessions)

Fuer Seiten die Login brauchen (X/Twitter, GitHub, etc.) den Chrome auf Port 18800 nutzen:
```bash
dev-browser --connect http://localhost:18800 <<'EOF'
const page = await browser.getPage("x");
await page.goto("https://x.com");
console.log(JSON.stringify({ url: page.url(), title: await page.title() }));
EOF
```

Dieser Browser laeuft permanent (LaunchAgent) und speichert Sessions zwischen Runs.
Einmalig manuell einloggen → danach nutzt Henry die gespeicherte Session.

Regeln:
- KEIN `openclaw browser` (Pairing-Problem)
- Externe Seiten mit Login → `--connect http://localhost:18800`
- Lokale Tools / oeffentliche Seiten → normales `dev-browser` (eigene Session)
- Bei 2x Timeout: `dev-browser stop` → retry → dann BLOCKED
- Named pages (`browser.getPage("name")`) bleiben zwischen Script-Runs erhalten
"""

    # ── Typische Abläufe (role-aware worked examples) ───────────────────
    # Ziel: Agents lernen durch konkrete Szenarien, nicht Command-Dumps.
    # Jeder Flow ist ein copy-paste-fähiger End-to-End-Ablauf mit realen
    # Beispiel-Inputs und zeigt welche Commands in welcher Reihenfolge für
    # eine typische Task-Art richtig sind.
    #
    # Design-Regel: worked example = ausgeführter Flow, nicht Command-Liste.
    # Kommentare im Block erklären Zweck jeder Zeile.
    flow_blocks: list[str] = []

    # Universal: Task-Lifecycle mit konkreten Beispielen
    flow_blocks.append(
        "### Ablauf 1 — Task empfangen und abschliessen (jede Rolle)\n\n"
        "```bash\n"
        "# 1. Orientieren: wer bin ich, was ist meine aktive Task\n"
        "mc me\n"
        "# → {\"id\": \"bc81...\", \"name\": \"...\", \"current_task\": {\"id\": \"c5e2...\", \"status\": \"inbox\"}, \"cli_skills\": [...]}\n"
        "\n"
        "# 2. ACK senden (Task → in_progress). Falls Response 409 \"In Progress → In Progress\":\n"
        "#    Du warst schon ACKed (via poll.sh direct-dispatch) — einfach weitermachen.\n"
        "mc ack\n"
        "\n"
        "# 3. Arbeit erledigen (siehe rollen-spezifische Abläufe unten)\n"
        "\n"
        "# 4. Zwischenstand dokumentieren (optional, bei mehrstufigen Tasks)\n"
        "mc comment \"Update — Phase 1 fertig. Starte Phase 2.\"\n"
        "\n"
        "# 5. Abschluss-Reflection + done\n"
        "mc comment --type reflection \"Was gemacht: ... / Was funktionierte: ... / Was unklar: ...\"\n"
        "mc done\n"
        "```\n"
        "\n"
        "**Wenn du unsicher bist** → `mc blocked --type <type> \"Was brauchst du?\"`:\n"
        "```bash\n"
        "mc blocked --type missing_info \"Welcher Tone-of-Voice? formal oder casual?\"\n"
        "# Task-Status → blocked, der Operator bekommt Telegram-Frage, du wartest auf Antwort\n"
        "```\n"
        "Valide blocker_type: `missing_info` | `technical_problem` | `decision_needed` | "
        "`permission_needed` | `dependency_blocked` | `other`."
    )

    # Chat-Write: Reporting-Flow mit konkreten File-Beispielen
    if _has(Scope.CHAT_WRITE):
        flow_blocks.append(
            "### Ablauf 2 — Report an den Operator über Telegram\n\n"
            "```bash\n"
            "# Einfacher Text-Report (Markdown unterstützt)\n"
            "mc telegram \"**Status** — Wetter-Recherche fertig. 3 Quellen kreuzvalidiert, Details im Deliverable.\"\n"
            "\n"
            "# Mit Bild (z.B. Screenshot, Chart, Mockup) — max 10 MB\n"
            "mc telegram \"Frontend-Mockup v2\" --photo /deliverables/$TASK_ID/mockup-v2.png\n"
            "\n"
            "# Mit Dokument (PDF, Word, Excel, ZIP) — max 50 MB\n"
            "mc telegram \"Wetter-Report KW17\" --file /shared-deliverables/$TASK_ID/report.pdf\n"
            "\n"
            "# Visual Verification (Screenshot + Metriken einer Live-URL)\n"
            "mc verify https://example.your-domain.com --caption \"Landingpage-Deploy verifiziert\"\n"
            "# → Sidecar macht Playwright-Screenshot + LCP/CLS + postet automatisch zu Telegram\n"
            "```"
        )

    # Tasks-Write: Deliverable + PDF-Flow mit realem Beispiel
    if _has(Scope.TASKS_WRITE):
        flow_blocks.append(
            "### Ablauf 3 — Ergebnis als Deliverable registrieren + PDF generieren\n\n"
            "```bash\n"
            "# Markdown-Report schreiben (Beispiel: Research-Deliverable)\n"
            "mkdir -p /deliverables/$TASK_ID\n"
            "cat > /deliverables/$TASK_ID/report.md <<'EOF'\n"
            "# Wetter-Report KW17 Zürich\n"
            "\n"
            "## Zusammenfassung\n"
            "Diese Woche regnet es am Mittwoch, sonst trocken.\n"
            "\n"
            "## Quellen\n"
            "- wetter.com (gefetcht 2026-04-24)\n"
            "- meteoblue.com\n"
            "EOF\n"
            "\n"
            "# Deliverable in DB registrieren (der Operator + andere Agents sehen es im UI)\n"
            "mc deliverable --type document --title \"Wetter-Report KW17\" --path /deliverables/$TASK_ID/report.md\n"
            "\n"
            "# Wenn der Operator ein PDF statt Markdown will: via mc-playwright Sidecar rendern\n"
            "mc pdf /deliverables/$TASK_ID/report.md --title \"Wetter-Report KW17\"\n"
            "# → /shared-deliverables/$TASK_ID/wetter-report-kw17.pdf (fuer Telegram-Anhang)\n"
            "\n"
            "# Zwischenstand speichern (falls Container-Restart → Task-Recovery hat Kontext)\n"
            "mc checkpoint \"Research fertig, PDF generiert, starte Telegram-Versand\"\n"
            "\n"
            "# Checkliste pflegen fuer Multi-Step-Tasks\n"
            "mc checklist add \"Research abgeschlossen\"\n"
            "mc checklist done <item-id>\n"
            "```"
        )

    # Tasks-Create (Orchestrator): Delegation-Flow mit Parent/Subtask
    if _has(Scope.TASKS_CREATE):
        flow_blocks.append(
            "### Ablauf 4 — Multi-Phase Task orchestrieren (Delegate + Callback warten)\n\n"
            "```bash\n"
            "# Parent-Task ist \"Erstelle Wetter-Report mit Telegram-Versand\" → drei Phasen:\n"
            "#   1. Recherche (Researcher)\n"
            "#   2. Content-Writing + Brand-Skill (Shakespeare)\n"
            "#   3. PDF + Telegram (FreeCode)\n"
            "\n"
            "# Phase 1 delegieren — atomic: erstellt Subtask + blockt Parent nicht mehr\n"
            "mc delegate \"Research: 7-Tage Wetter Zürich\" \\\n"
            "  --to Researcher \\\n"
            "  --description \"Kreuzvalidiere mind. 3 Quellen. Registriere Markdown-Deliverable mit Temperatur min/max + Niederschlag pro Tag.\"\n"
            "# → Subtask mit callback_agent_id=du erstellt\n"
            "\n"
            "# WARTEN: Du bleibst 'in_progress' (NICHT blocked!). subtask_completed Comment kommt im naechsten Poll.\n"
            "# Wenn du blocked setzt: der Operator kann den Blocker nicht sinnvoll resolven (nichts fuer ihn zu tun),\n"
            "# Task haengt bis manueller Unblock. Callback-Waits sind immer in_progress.\n"
            "\n"
            "# Nach subtask_completed Comment: Phase 2 delegieren mit Deliverable-Verweis aus Phase 1\n"
            "mc delegate \"Content: Formatierter Wetter-Bericht mit Brand-Voice\" \\\n"
            "  --to Shakespeare \\\n"
            "  --description \"Nutze Research-Deliverable <uuid> aus Phase 1. Skill: client-brand-skill. Formal 'Sie', Primary-Color #005850.\"\n"
            "\n"
            "# Wenn alle Phasen fertig: status → review, Final-Report an den Operator\n"
            "mc telegram \"Multi-Phase Wetter-Report komplett. Siehe Deliverables.\" --file /shared-deliverables/$TASK_ID/final.pdf\n"
            "mc done\n"
            "```"
        )

    # Plugin-Management (Board Lead): komplettes Discovery + Install + Assign Flow
    if is_board_lead and _has(Scope.AGENTS_MANAGE):
        flow_blocks.append(
            "### Ablauf 5 — Worker mit neuem Tool/Skill ausstatten (Board Lead)\n\n"
            "```bash\n"
            "# Szenario: Davinci scheitert 2× an einem Video-Task — vermutlich fehlendes Tool.\n"
            "\n"
            "# 1. Check: was hat Davinci heute?\n"
            "mc plugin-show Davinci\n"
            "# → {\"agent_name\": \"Davinci\", \"cli_plugins\": null}  (null = alle installierten)\n"
            "\n"
            "# 2. Check: welche Plugins existieren im Shared Cache?\n"
            "mc plugin-list\n"
            "# → {\"plugins\": [{\"key\": \"higgsfield-mcp@anthropic-agent-skills\", ...}, ...]}\n"
            "\n"
            "# Wenn das gewuenschte Plugin schon da ist: direkt zuweisen\n"
            "mc plugin-assign Davinci higgsfield-mcp@anthropic-agent-skills --restart\n"
            "# → Plugin in Davinci's cli_plugins gesetzt, claude-Session in tmux reload\n"
            "\n"
            "# Wenn NICHT da: Install-Request mit Operator-Approval stellen\n"
            "curl -sf -X POST \"$MC_API_URL/api/v1/agent/install-requests\" \\\n"
            "  -H \"Authorization: Bearer $MC_AGENT_TOKEN\" \\\n"
            "  -H \"Content-Type: application/json\" \\\n"
            "  -d '{{\n"
            "    \"type\": \"mcp\",\n"
            "    \"operation\": \"install\",\n"
            "    \"source\": \"github:geopopos/higgsfield_ai_mcp\",\n"
            "    \"name\": \"higgsfield-ai\",\n"
            "    \"target_agent_id\": \"<davinci-uuid>\",\n"
            "    \"reason\": \"Davinci failed 2 video-tasks — braucht Higgsfield-MCP\",\n"
            "    \"task_id\": \"'\"$TASK_ID\"'\"\n"
            "  }}'\n"
            "# task_id-Koppelung → bei Approval postet Backend install_completed Comment auf DEINE Task.\n"
            "# Du wartest in_progress bis Comment kommt. InstallExecutor setzt cli_plugins automatisch.\n"
            "\n"
            "# Worker manuell reloaden (wenn Plugin ohne --restart zugewiesen)\n"
            "mc worker-restart Davinci\n"
            "```"
        )

    # Knowledge/Memory: Semantic Search + Write Flow
    if _has(Scope.KNOWLEDGE_READ):
        flow_blocks.append(
            "### Ablauf 6 — Kontext aus frueheren Tasks finden (Knowledge-Base)\n\n"
            "```bash\n"
            "# Semantische Suche ueber Qdrant + Board-Memory\n"
            "mc memory \"client brand guidelines primary color\"\n"
            "# → Top-K ähnliche Eintraege mit content + score + memory_type\n"
            "\n"
            "# Wenn du etwas wichtiges gelernt hast: zurueckschreiben\n"
            "curl -sf -X POST \"$MC_API_URL/api/v1/agent/knowledge\" \\\n"
            "  -H \"Authorization: Bearer $MC_AGENT_TOKEN\" \\\n"
            "  -H \"Content-Type: application/json\" \\\n"
            "  -d '{{\n"
            "    \"content\": \"Dispatch-Messages >8000 chars sind kontraproduktiv (lost-in-middle)\",\n"
            "    \"memory_type\": \"lesson\",\n"
            "    \"scope\": \"board\"\n"
            "  }}'\n"
            "# scope: \"agent\" = nur ich | \"board\" = alle im Board | \"global\" = alle Agents\n"
            "```"
        )

    quick_ref = "\n\n".join(flow_blocks)

    # ── Zusammenbauen ────────────────────────────────────────────────────
    sections = [s for s in [knowledge_section, vault_section, vault_remember_section, memory_section, heartbeat_section, help_request_section, install_request_section, credentials_section, deploy_section] if s]
    main_body = "\n\n".join(sections)

    return f"""# {emoji} {name} — Mission Control Tools

## Authentifizierung

Alle Requests brauchen den Authorization-Header:
  Authorization: Bearer $MC_AGENT_TOKEN

API Base: http://localhost

---

## Typische Abläufe — copy-paste-fähige Tool-Call-Beispiele

Konkrete Szenarien mit realen Inputs. Jeder Flow ist ein End-to-End-Ablauf:
welche Commands in welcher Reihenfolge für welche Situation. Die raw-curl-
Form jedes Endpoints findest du weiter unten in den Detail-Sektionen.
Übersicht aller `mc`-Commands: `mc --help` bzw `mc <cmd> --help`.

{quick_ref}

---

{main_body}
{board_section}{content_section}{board_lead_section}{browser_section}
---
Generiert automatisch beim Erstellen des Agents.
"""
