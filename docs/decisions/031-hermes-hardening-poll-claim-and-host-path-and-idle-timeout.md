# ADR-031: Hermes Hardening — Poll-Claim Semantic, Per-Agent Idle Timeout, Deliverable Dual-Path

**Status:** Accepted
**Datum:** 2026-05-01
**Scope:** Backend/Dispatch · Backend/Watchdog · Backend/Agent-Scoped · Infra/Host-Worker
**Related:** ADR-030 (Hermes Autonomous-Worker Config), ADR-029 (Hermes Host-Side tmux Worker), Migration 0018 (Dispatch ACK Handshake), Migration 0096 (ack_timeout_minutes), Migration 0097 (idle_timeout_minutes), Migration 0098 (deliverable agent_id nullable)

---

## Kontext

Phase 26 Smoke (2026-05-01) auf Hermes deckte drei strukturelle Lücken auf, die alle dieselbe Wurzel teilen: MC's Lifecycle-, Timeout- und Deliverable-Path-Code enthielt Annahmen die nur für Docker-Worker gelten — Hermes läuft aber auf dem Host.

ADR-030 hatte den autonomen Worker-Config-Bündel etabliert (board_id auto-assign, --yolo, env_passthrough, MCP-first). Was ADR-030 nicht berührt hat: **wie der Backend-Server den Task-Lifecycle für einen host-side Worker handhabt** und **wie Dateipfade auf dem Host registriert werden**.

Drei strukturelle Probleme, alle aus derselben Phase-26-Smoke sichtbar:

1. **F1/F2/F3 — Poll-Claim kollabiert den ACK-Handshake zu einem einzigen Moment.**
   `GET /agent/me/poll` schrieb beim Inbox-Claim atomar `status=in_progress + dispatched_at=now + ack_at=now`. Das verstieß gegen Migration 0018 (Dispatch ACK Handshake): Status wechselte schon bevor die LLM-Session den Prompt gesehen hatte (F1), `dispatched_at == ack_at` ohne messbare Spanne (F3), und `started_at` blieb NULL weil der PATCH-Pfad nie erreicht wurde (F2).

2. **F6 — Global Idle-Timeout killt Deployer/heavy-Coder mitten in der Arbeit.**
   Der Watchdog nutzte einen globalen `STALE_PROGRESS_MINUTES` für alle Agents. Deployer-Tasks beinhalten npm install (3–5min) + next build (2–4min) + Vercel-Deploy (1–3min) + DNS — routinemäßig über 10 Minuten ohne sichtbaren Status-Update. Migration 0096 hatte bereits `ack_timeout_minutes` als per-Agent-Override eingeführt; das sibling pattern für Idle-Timeout fehlte.

3. **F8 — Deliverable-Path-Validator akzeptiert nur Docker-interne Mount-Pfade.**
   Hermes (Host-Worker) erzeugt Dateien unter `~/.mc/deliverables/{task_id}/`. Der Validator kannte nur `/deliverables/{task_id}/` (Docker-internes Mount-Ziel) und `/shared-mcp/{task_id}/`. Hermes bekam 422, fiel auf `document`-Type mit Inline-Content zurück, und das physisch vorhandene PDF landete nicht als File-Deliverable im System.

---

## Entscheidung 1 — Poll-Claim reserviert, flippt aber nicht den Status

### Was

`GET /api/v1/agent/me/poll` setzt beim Inbox-Claim **nur** `dispatched_at` (wenn noch NULL) und den `current_task_id`-Lock. Status bleibt `inbox`. `ack_at` und `started_at` werden **nicht** im poll-Pfad gesetzt.

Der Agent's eigener `PATCH /api/v1/agent/tasks/{id}` mit `status=in_progress` ist der einzige Pfad der **atomar** setzt: `status=in_progress`, `ack_at=now()`, `started_at=now()` (via tasks.py:1239-1241 — already correct since Migration 0018).

**Vorher** (atomarer, fehlerhafte Write):
```python
was_inbox = task.status == "inbox"
if was_inbox:
    task.status = "in_progress"      # F1: Status leckt nach oben
    task.dispatched_at = now
task.ack_at = now                    # F3: identischer now-Literal, Spanne = 0
```

**Nachher** (Split-Write):
```python
was_inbox = task.status == "inbox"
if was_inbox:
    # F1 fix: Status bleibt "inbox" bis der Agent selbst PATCH schickt
    if task.dispatched_at is None:
        task.dispatched_at = now
    # NOTE: ack_at NICHT setzen. status NICHT setzen.
else:
    # in_progress/review/blocked Recovery-Pfad (bereits-geackte Tasks)
    task.ack_at = now
```

Response-Payload um `task.status`, `task.dispatched_at`, `task.ack_at` erweitert (Beobachtbarkeit — kein Consumer musste angepasst werden, alle nutzen `task.id` aus der Antwort).

### Warum

Migration 0018 definiert den ACK-Handshake als bewusste Zeitspanne: Dispatch (Server sendet Prompt) → `dispatched_at`; Agent bestätigt Empfang → `ack_at`. Diese Spanne ist menschlich bedeutsam: "Hat der Agent die Aufgabe überhaupt gesehen?" Ein poll-claim der beides gleichzeitig setzt (mit demselben `datetime.now()` Literal) kollabiert diese Spanne auf exakt 0ms und macht das Feld wertlos als Diagnose-Signal.

Hermes' Bridge-Deduplizierung (`_last_dispatched_task_id` Cache) verhindert Re-Dispatch in tmux solange der Task noch auf ACK wartet. Docker-Agents (`LAST_DISPATCHED_TASK_ID` in poll.sh) haben denselben Guard. Kein Consumer ist auf den Status-Wechsel im poll-Response angewiesen — alle Bridges lesen `task.id` + `task.title` + `task.prompt`.

### Alternativen

- **`claimed_at` als neuer Lifecycle-Status zwischen `inbox` und `in_progress`:** Würde den 5-stufigen Status-Flow erweitern, alle UIs + Watchdog-Guards anpassen erfordern, und einen völlig neuen Status für ein Problem einführen das der bestehende ACK-Handshake bereits modelliert. Verworfen — overkill.
- **`ack_at = dispatched_at + 1ms` künstlich setzen:** Dishonest — die Spanne soll die Realität spiegeln (Zeit bis der Agent die Dispatch-Message prozessiert), nicht eine Konstante sein. Verworfen.
- **Bridge setzt `dispatched_at` statt Backend:** Würde das Bridging-Protokoll complexer machen (Bridge braucht extra PATCH call) ohne einen Vorteil. Backend ist der richtige Ort für Lifecycle-Timestamps.

### Implementation

- `backend/app/routers/agents.py:2944-3055` (Poll-Endpoint, Plan 26-02)
- Commits: `96d568cc` (RED tests), `5d36d36d` (GREEN implementation)
- Tests: `test_hermes_lifecycle_hardening.py` (3 tests: F1/F3/F2), `test_hermes_bridge.py::test_dispatch_then_ack_timestamps_diverge`

---

## Entscheidung 2 — Per-Agent `idle_timeout_minutes` als Sibling zu `ack_timeout_minutes`

### Was

`agents.dispatch_config` (JSON column) bekommt einen neuen Key `idle_timeout_minutes`. `task_runner._idle_threshold_for(agent)` liest in 4-stufiger Priorität:

1. `dispatch_config["idle_timeout_minutes"]` — **NEU, Migration 0097**
2. `dispatch_config["stale_progress_minutes"]` — bestehend, backwards-compat
3. `STALE_PROGRESS_MINUTES_BY_ROLE` — rollenbasierter Default
4. `STALE_PROGRESS_MINUTES` — globaler Hard-Fallback (60min)

Migration 0097 setzt initial:
- Deployer: 30 Minuten
- FreeCode: 20 Minuten
- Davinci: 20 Minuten
- Neo: 20 Minuten (WARN + Skip wenn Agent nicht in DB — idempotent by design, analog zu Migration 0096)
- Alle anderen Agents: kein Eintrag → fallback auf bestehende Defaults

### Warum

Migration 0096 etablierte `ack_timeout_minutes` als per-Agent Override für ACK-Timeouts. Das Muster ist bekannt, getestet, und DB-driven (kein Redeploy für Anpassungen). `idle_timeout_minutes` als sibling-key folgt exakt demselben Muster — gleiche Lookup-Funktion, gleiche fallback-Kette, gleiche Idempotenz-Eigenschaft der Migration.

Alternative "globalen Default auf 30min bumpen" würde die Stale-Detection für schnelle Workers (Rex, Shakespeare, Tester) unnötig verlangsamen und Bugs in diesen Agents länger unentdeckt lassen. Per-Agent ist das ehrlichere Modell: Deployer braucht 30min, Reviewer braucht 10min.

### Alternativen

- **Globalen `STALE_PROGRESS_MINUTES` erhöhen:** Einfach, aber zu breit — verlangsamt Stale-Detection für alle schnellen Agents. Verworfen.
- **Neues Boolean-Feld `long_running_agent: bool`:** Weniger flexibel als Minuten-Wert (kein graduelles Tuning), würde ein neues Migrations-Pattern einführen obwohl das JSON-dispatch_config Pattern bereits vorhanden ist. Verworfen.
- **Task-seitiger Override (per-Task idle_timeout):** Sinnvoll für extrem lange Tasks, aber komplexer (Task-Schema erweitern, Dispatch-Protokoll erweitern). Als Future-Enhancement offen, nicht für Phase 26 notwendig.

### Implementation

- `backend/alembic/versions/0097_per_agent_idle_timeout_minutes.py` — Migration (Plan 26-06)
- `backend/app/services/task_runner.py:67-89` — `_idle_threshold_for(agent)` Erweiterung
- Commit: `293dd98e`
- Tests: `test_idle_timeout_hardening.py` (2 tests), `test_hermes_dispatch_config.py` (5 helper-level tests)

**Hinweis für künftige per-Agent Timeout-Knöpfe:** Das Muster ist jetzt etabliert — neuer Key in `dispatch_config`, neue Lookup-Priorität in der jeweiligen `_threshold_for(agent)` Funktion, idempotente Migration die bestehende Agents updated. Keine Schema-Migration auf `agents` selbst nötig.

---

## Entscheidung 3 — Deliverable-Validator akzeptiert Host-Form und Docker-Form

### Was

`accepted_prefixes` im Deliverable-POST-Validator (`agent_scoped.py:1289-1340`) wird erweitert von:

```python
accepted_prefixes = (agent_prefix, mcp_prefix)  # Docker-only
```

auf:

```python
home_host = os.environ.get("HOME_HOST", os.path.expanduser("~"))
host_tilde_prefix    = f"~/.mc/deliverables/{task_id}/"
host_resolved_prefix = f"{home_host}/.mc/deliverables/{task_id}/"
accepted_prefixes = (agent_prefix, mcp_prefix,
                     host_tilde_prefix, host_resolved_prefix)
```

Der FileResponse-Resolver (`tasks.py:_resolve_deliverable_fs_path`) mapped Host-Form auf Docker-interne Form **bevor** der per-Agent-Slug-Expansion:

```python
_stored = deliverable.path
if _stored.startswith("~/.mc/deliverables/"):
    _stored = "/deliverables/" + _stored[len("~/.mc/deliverables/"):]
elif _stored.startswith(f"{home_host}/.mc/deliverables/"):
    _stored = "/deliverables/" + _stored[len(f"{home_host}/.mc/deliverables/"):]
```

Der ORM-Object (`deliverable.path`) wird nie mutiert — die DB speichert den originalen Host-Pfad wie der Agent ihn geschrieben hat. Das Mapping ist rein serverseitig für die Datei-Auslieferung via `FileResponse`.

Path-Traversal-Schutz (`os.path.normpath` + Prefix-Recheck) gilt für **alle vier** Prefix-Formen — es gibt keinen Bypass durch Form-Wechsel.

Parallel dazu: `task_deliverables.agent_id` wird nullable (Migration 0098) und ein spiegelgleicher Admin-POST-Endpoint unter `tasks.py` für den MCP-Pfad (`mc_register_deliverable` via admin JWT) wird eingeführt — sodass Hermes' MCP-Tool einen 201-Response bekommt statt 405 (Plan 26-04, HERM-11/F4).

### Warum

Die physische Datei ist dieselbe — `~/.mc/deliverables/` auf dem Host ist via Docker-Volume-Mount identisch mit `/deliverables/` im Backend-Container. Hermes weiß aber nur den Host-Pfad (wo es läuft). Es zu zwingen, den Docker-internen Pfad zu kennen, wäre eine leakende Abstraktion: ein Host-Worker müsste verstehen wie sein Dateisystem im Backend-Container ausschaut.

Beide Pfadformen zu akzeptieren ist ehrlich gegenüber der tatsächlichen Runtime-Topologie. Der Backend-Resolver ist der richtige Ort für das Mapping, weil:
1. Er bereits der einzige Integration-Punkt für alle 4 FileResponse-Handler ist (image/file/directory/open)
2. Das Mapping vor der per-Agent-Slug-Expansion stattfinden muss (bestehende Logik bleibt unverändert)
3. Kein Consumer (Hermes Bridge, mc-mcp.py) muss geändert werden

### Alternativen

- **Hermes kopiert Dateien in Docker-gemounteten Pfad:** Friction — Hermes müsste `/deliverables/` auf dem Host kennen und schreiben-können. Gleiche Quelle, unnötige Kopie. Verworfen.
- **Backend-Transparentes Rewrite ohne Whitelist-Erweiterung:** Das Rewrite im Resolver allein genügt für `FileResponse`, aber der Validator würde weiterhin 422 liefern (er kennt die Host-Form nicht). Beide Ebenen müssen angepasst werden für ein konsistentes API-Verhalten. Verworfen (halbherzig).
- **Separater Endpoint für Host-Worker-Deliverables:** Unnötige API-Oberfläche, kein Mehrwert. Ein einziger Endpoint der beide Formen akzeptiert ist einfacher. Verworfen.
- **Expliziter `serve_path` Helper (Plan-Vorschlag):** Der Plan schlug `_resolve_deliverable_serve_path()` als separaten Helper vor. Verworfen zugunsten von Inlining im bestehenden `_resolve_deliverable_fs_path` Resolver — der bereits der einzige Aufrufpunkt für alle 4 FileResponse-Handler ist, und das Mapping muss vor der Slug-Expansion stattfinden. Keine Verhaltensänderung.

### Implementation

- `backend/app/routers/agent_scoped.py:1289-1340` — Validator, HOST_HOST-Erweiterung (Plan 26-08)
- `backend/app/routers/tasks.py` — `_resolve_deliverable_fs_path` + Admin-POST-Route (Plan 26-04 + Plan 26-08)
- `backend/alembic/versions/0098_deliverable_agent_id_nullable.py` — agent_id nullable (Plan 26-04)
- Commits: `698c999f` (Validator + Resolver, Plan 26-08), `55f3b4e9` (Admin-Route + Migration 0098, Plan 26-04)
- Tests: `test_agent_scoped_deliverables.py` (8 tests), `test_mc_mcp_routes.py` (6 neue Tests für F4)

---

## Konsequenzen

### Positiv

- **Zukünftige Host-Worker erben alle drei Patterns kostenlos.** Ein neuer host-side Worker bekommt Poll-Claim-Split (bereits im Backend), `idle_timeout_minutes` in `dispatch_config` (per-Agent configurierbar), und Deliverable-Host-Path-Support (Validator kennt tilde + HOME_HOST) ohne neue Code-Änderungen.
- **Migration 0018 ACK-Handshake ist wiederhergestellt.** `dispatched_at < ack_at` ist jetzt eine invariante Eigenschaft des Systems — nicht nur ein Versprechen das vom Poll-Pfad gebrochen werden konnte.
- **Per-Agent dispatch_config ist ein etabliertes Erweiterungsmuster.** `ack_timeout_minutes` (Migration 0096) + `idle_timeout_minutes` (Migration 0097) zeigen: neue per-Agent Timeout-Knöpfe können ohne Schema-Migration auf `agents` selbst hinzugefügt werden.
- **Path-Traversal-Schutz ist konsistent** — keine asymmetrische Sicherheit zwischen Docker-Form und Host-Form.

### Negativ

- **Poll-Response gibt jetzt `status`, `dispatched_at`, `ack_at` zurück.** Bridge-Consumers, die diese Felder lesen, müssten den neuen Status-Wert verstehen. Aktuell liest kein Consumer diese Felder (verifiziert via grep) — aber zukünftige Bridge-Updates müssen das beachten.
- **Zwei Deliverable-Pfad-Validator-Implementierungen** (agent_scoped.py + tasks.py Admin-Route) müssen synchron gehalten werden wenn sich die Prefixes ändern. Beide sind via Docstring-Querverweise verbunden ("Mirrors agent_scoped.py POST"). Ein dritter Caller würde eine shared `services/deliverables.py` Extraktion rechtfertigen.
- **`dispatch_config`-Schema ist undokumentiert.** Bekannte Keys: `ack_timeout_minutes`, `idle_timeout_minutes`, `stale_progress_minutes`. Keine JSON-Schema-Validierung — fehlerhafte Keys werden still ignoriert. Future-Enhancement: Pydantic-Modell für dispatch_config.

---

## Referenzen

### Betroffene Dateien

- `backend/app/routers/agents.py:2944-3055` — Poll-Endpoint Split-Write
- `backend/app/routers/agent_scoped.py:1289-1340` — Deliverable-Validator accepted_prefixes
- `backend/app/routers/tasks.py` — Admin-POST-Route + `_resolve_deliverable_fs_path` Host-Mapping
- `backend/app/services/task_runner.py:67-89` — `_idle_threshold_for(agent)`
- `backend/alembic/versions/0097_per_agent_idle_timeout_minutes.py`
- `backend/alembic/versions/0098_deliverable_agent_id_nullable.py`
- `scripts/hermes-bridge.py` — Bridge crash-safe try/except + SIGTERM handler (Plan 26-05)
- `docker/hermes/com.mc.hermes-bridge.plist` — KeepAlive: true (Plan 26-05)

### Commits

- `96d568cc` — RED stub tests (Plan 26-01)
- `5d36d36d` — Poll-Claim Split GREEN (Plan 26-02)
- `293dd98e` — per-agent idle_timeout_minutes, Migration 0097 (Plan 26-06)
- `55f3b4e9` — mc_register_deliverable admin route + Migration 0098 (Plan 26-04)
- `698c999f` — Deliverable validator + FileResponse host-path resolver (Plan 26-08)
- `a7e99984` — Bridge crash-safe main loop + plist KeepAlive:true (Plan 26-05)

### Verwandte ADRs

- **ADR-029** — Hermes als host-side tmux Worker (Phase 24 Fundament)
- **ADR-030** — Hermes Autonomous-Worker Config (Phase 25, direkt vorausgehend)
- **Migration 0018** — Dispatch ACK Handshake (Entscheidung 1 stellt dessen Intent wieder her)
- **Migration 0096** — `ack_timeout_minutes` per-Agent (Entscheidung 2 mirrors its shape)
