# ADR-020 — Harness Phase 2: `mc` CLI + Dispatch Split + Progress SSoT

**Status:** Accepted
**Datum:** 2026-04-20
**Scope:** Backend/Dispatch · Infra/Runtime · Agent Protocol
**Supersedes partially:** ADR-007 (Structured Dispatch Messages) — curl-
blocks are out, CLI-commands are in

## Kontext

Nach der Claude-Fleet-Migration (ADR-019) hatten die Dispatch-Messages
wieder eine Tendenz zum Wachstum: ACK-Block + Kommentar-Protokoll +
Callback-Protokoll + Help-Request + Clarification + Error-Recovery —
zusammen etwa 2000 Zeichen reines curl-Boilerplate pro Task-Prompt.
Plus: drei parallele Progress-Tracking-Systeme (TaskCheckpoint,
`comment_type='checkpoint'`, TaskChecklistItem) mit überlappender
Semantik. Und: bei grossen Dispatch-Messages trat "Lost in the Middle"
auf — wichtige Task-Details wurden bei Sonnet-4.6 schlechter erfasst.

Drei Anforderungen:

1. **Dispatch schlanker machen.** Agent sollte ~1500-2000 Zeichen
   Task-Prompt bekommen, nicht ~6000-8000.
2. **Progress-Tracking konsolidieren.** Ein Ort für "was ist erledigt,
   was steht an" — keine drei parallelen Systeme.
3. **Dispatch-Prompt muss bei neuen API-Endpoints nicht wachsen.** Eine
   CLI kann wachsen, ohne dass jeder Task-Prompt länger wird.

## Entscheidung

Drei gekoppelte Änderungen:

### A1 — `mc` CLI (stdlib-only Python)

Alle agent-scoped Backend-Endpoints, die Worker-Lifecycle-Aktionen
abdecken, sind über einen 450-Zeilen CLI-Wrapper im Image erreichbar:

```
mc ack                      # PATCH status=in_progress
mc done / mc review         # PATCH status=done | review
mc blocked --question "..." # PATCH status=blocked + blocker_fields
mc failed --reason "..."    # comment + PATCH status=failed
mc comment <type> "..."     # POST /comments
mc checklist add/done/list  # Checklist CRUD
mc deliverable --title ...  # POST /deliverables
mc question "..."           # POST /clarification
mc help <role> --title ...  # POST /help-request
mc memory search "..."      # GET /me/memory/search (A3)
```

CLI liegt unter `/home/agent/.local/bin/mc` in beiden Agent-Images
(`mc-claude-agent` + `mc-agent-base` für Sparky). Env-Context
(`TASK_ID`, `BOARD_ID`, `X_DISPATCH_ATTEMPT_ID`) wird von `poll.sh` via
`tmux set-environment` + `/tmp/mc-context.env` bereitgestellt; die CLI
liest beide. Retry 3x bei 5xx, hard-fail bei 4xx, klare Exit-Codes.

Ein **Generator-Test** (`backend/tests/test_mc_cli_endpoints.py`)
bricht CI, sobald ein neuer agent-scoped Endpoint weder ein
CommandSpec noch einen expliziten `SKIP_CLI`-Eintrag mit Begründung
hat. Damit bleibt die CLI zwingend synchron mit dem Backend.

### A2 — Dispatch-Message Slim mit 3-Zonen-Budget

Aus der Dispatch-Message rausgeflogen (in SOUL gewandert): ACK-Block,
Kommentar-Protokoll, Callback-Protokoll, Help-Request, Clarification,
Error-Recovery. Rein: ein kompakter "Lifecycle"-Block mit `mc`-Refs.

Grössen-Budget:
- **Target 2000 Zeichen** (optimal)
- **Warn 2500 Zeichen** (Log-Warning)
- **Hard 4000 Zeichen** (Graceful Degradation: optionale Sektionen
  droppen in Priorität, Mandatory-Sektionen nie abschneiden)

Measure-only Logging eingebaut; echte DispatchSection-Assembly kommt
als Follow-up.

### A3 — Memory On-Demand

`_load_dispatch_context` attachiert nur noch Top-3 Qdrant-Treffer (max
800 Zeichen) automatisch. Board Memory, Agent Lessons, Keyword
Lessons, Team Lessons, Intelligence, Meeting Context werden NICHT mehr
auto-injiziert. Agent holt sich mehr via `mc memory search "keyword"`
— neuer GET-Endpoint `/api/v1/agent/me/memory/search`.

### A4 — TaskChecklistItem als Single Source of Truth für Progress

- `comment_type='checkpoint'` → migriert zu `'progress'` (Migration 0082)
- `POST /checkpoint` → **HTTP 410 Gone** (Route bleibt 2 Releases
  registriert für Legacy-Clients, dann DELETE)
- `TaskCheckpoint` Tabelle bleibt als read-only Archiv bis 3 Wochen
  nach Rollout, dann DROP.
- `build_recovery_context()` refactored: liest nur noch
  TaskChecklistItem + die letzten 5 progress/blocker/feedback/
  resolution-Comments, kompaktes Format mit `← HIER WEITERMACHEN`
  Marker.

## Alternativen

- **MCP-Server statt CLI:** Eleganter theoretisch, aber jeder
  Container bräuchte einen zusätzlichen Subprocess, dispatch hätte
  MCP-spezifische Call-Rendering. CLI ist 1-Binary-Deploy via
  existing image-build, kein MCP-Protokoll-Overhead. Wird später als
  separates ADR re-evaluated wenn MCP-Server-Tooling reifer ist.
- **Dispatch bleibt komplett inline-curl:** Verworfen — Lost-in-the-
  Middle-Effekt bei wachsenden Prompts ist empirisch messbar.
- **Progress-Tracking über TaskCheckpoint-Only:** Verworfen —
  TaskChecklistItem hat 163 reale Nutzungen, TaskCheckpoint nur 8.
  Major direction of travel war schon pro-Checklist.

## Konsequenzen

### Positiv

- **Dispatch-Prompt ~50-70% kleiner** für Worker-Tasks.
- **CLI wächst billig mit dem Backend** — dispatch-Prompt bleibt
  konstant, auch wenn 20 neue Endpoints dazukommen.
- **Ein Fortschritts-System** — Checkliste ist auditable,
  visualisierbar, LLM-lesbar.
- **Recovery-Prompt ist gezielter** — `← HIER WEITERMACHEN` Marker
  statt 10-Kommentar-Wall.
- **Memory ist on-demand** — Agenten ziehen was sie brauchen, statt
  dass wir spekulativ 2000 Zeichen Kontext mitschicken.

### Negativ

- **Rollout braucht Image-Rebuild** (`scripts/build-agent-images.sh`
  + `docker compose up --recreate-containers`). Bis Reprovisioning
  (Workstream G) läuft: neue Dispatches verweisen auf `mc`, aber live
  Agenten im alten Image finden den Befehl nicht. Hybrid-Phase ist
  sicher weil die alten SOULs noch die curl-Referenzen haben.
- **`mc memory search` ist eine neue Abhängigkeit** — wenn Qdrant
  down, gibt die CLI einen 5xx-Retry und dann Fehler. Backend muss
  robust gegen das sein (Fail-Soft in memory_query.py reicht aktuell).
- **TaskCheckpoint wird 3 Wochen parallel gehalten** — minimaler
  Technischer Ballast für Rollback-Safety.

## Rollout

1. Migration `0082` (deprecate checkpoint) → `0083` (Henry scopes) →
   `0084` (soul_persona_md) → `0085` (persona seed).
2. Image-Build + `sync-config` pro Agent → neue SOUL + neue
   Dispatch-Messages.
3. Observe: `dispatch_size` log metric. Erwartung: Worker-Prompts
   durchweg unter 2500 Zeichen.
4. Nach 2 Releases: `POST /checkpoint` Route löschen.
5. Nach 3 Wochen: `task_checkpoints` Tabelle droppen.

## Referenzen

- PRs: #47 (Workstream A), #48 (B+C), #49 (D-prep + F), #50 (D-full + E + G)
- Plan: `docs/superpowers/plans/2026-04-20-harness-personas-session-handoff.md`
- Key files:
  - `scripts/mc-cli/` — CLI package
  - `scripts/build-agent-images.sh` — image sync + build
  - `backend/app/services/dispatch.py` — `_assemble_with_budget`, `build_recovery_context`
  - `backend/app/routers/agent_scoped.py` — `GET /me/memory/search`, `POST /checkpoint` → 410
  - `backend/alembic/versions/0082_deprecate_checkpoint_comments.py`
  - `backend/tests/test_mc_cli_endpoints.py` — generator test
- Verwandte ADRs: ADR-007 (Structured Dispatch, teil-superseded),
  ADR-019 (Claude Fleet Hybrid), ADR-021 (Agent Personas)
