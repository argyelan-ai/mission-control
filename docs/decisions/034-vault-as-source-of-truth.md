# ADR-034 — Vault as Source of Truth (Karpathy-Wiki Memory)

**Status:** Accepted (M.1-M.5 live, merged to main as `0b35ed83` on 2026-05-15)
**Datum:** 2026-05-14 (Proposed) · 2026-05-15 (Accepted)
**Scope:** Backend/Memory · Backend/Services · Infra/Storage · Agent Protocol

## Kontext

### Heutiger Zustand (Phase 7 — 1-Way Export)

Vor diesem ADR ist `board_memory` (PostgreSQL-Tabelle) der alleinige Wissens-Speicher fuer Agents, Boards und Global Knowledge. Phase 7 (Obsidian View-Only Export) hat eine einseitige Spiegelung nach `~/.mc/vault/` aufgebaut: `ObsidianExportService` rendert BoardMemory-Zeilen als Markdown-Dateien in den Vault-Ordner.

**Das strukturelle Problem:** Agents koennen nicht direkt in den Vault schreiben. Jeder Wissenseintrag muss via REST-API (`POST /knowledge`) durch das Backend gehen. Das macht agenten-seitige Erkenntnisse zu einem Fernzugriffs-Protokoll statt zu einem natuerlichen Schreibfluss.

Konsequenzen daraus:
- Lessons und Decisions entstehen erst wenn der Agent einen API-Call macht — "aus dem Kontext fallen" (z.B. Context-Compaction, Container-Restart) vernichtet ungespeicherte Erkenntnisse
- Obsidian-Ansicht ist read-only aus Agenten-Perspektive — der Wissensgraph entsteht im Backend, nicht beim Agenten
- Kein echtes Agent-Ownership von Wissen — `agents/{slug}/` Directory existiert, aber nur als Spiegel

### Karpathy-Muster als Losung

Andrej Karpathy's Blog-Post-Stil: Markdown-Dateien in einem Git-Repo als primarer Wissensspeicher. "llmwiki"-Pattern: SQLite FTS5 als rebuild-barer Index ueber ein Filesystem. Kein Datenverlust bei Index-Verlust (der Vault ist Source of Truth, nicht der Index).

**MC Memory Vault**: Jeder Agent hat `~/.mc/vault/agents/{slug}/` als eigenen Schreibraum. Cross-agent Kommunikation via `~/.mc/vault/_inbox/{target}/`. Backend ist Watcher + Kompaktierungs-Service, nicht primarer Speicher.

**Spec:** `docs/superpowers/specs/2026-05-14-mc-memory-vault-as-source-design.md`

## Entscheidung

**Markdown Vault (`~/.mc/vault/`) wird Source of Truth fuer Lessons, Decisions, Knowledge, Concepts.** Backend (`board_memory` Tabelle + PostgreSQL) ist Watcher, Inbox-Compactor und Kompatibilitaets-Shim waehrend der Migration — nicht mehr primarer Schreibpfad.

### M.1 — Read Foundation (2026-05-14, diese Implementierung)

M.1 implementiert die Lese-Infrastruktur und den Watcher:

| Service | Datei | Zweck |
|---|---|---|
| `VaultIndex` | `services/vault_index.py` | SQLite FTS5 (`.mc_index.db`), Upsert + Search, rebuild_from_vault() |
| `VaultActivity` | `services/vault_activity.py` | Redis Sorted-Set Heatmap, track_write(), get_hot_paths() |
| `VaultGit` | `services/vault_git.py` | Stub fuer M.2 (Commit + Push per Agent-Write) |
| `VaultEmbeddings` | `services/vault_embeddings.py` | No-op Stub fuer M.1 — volle DGX→Qdrant-Verdrahtung in M.2 |
| `VaultWatcher` | `services/vault_watcher.py` | `watchfiles`-basierter FS-Watcher, validiert + indexiert neue/geaenderte .md-Dateien, quarantaeniert invalide |
| `vault.py` (Router) | `routers/vault.py` | `GET /api/v1/vault/notes`, `/search`, `/note/{path}` — User-JWT Auth |

**Neue API-Endpoints (M.1):**
- `GET /api/v1/vault/notes?agent=&type=&limit=&offset=` — Paginierte Vault-Notizen aus FTS-Index
- `GET /api/v1/vault/search?q=&agent=&type=&limit=` — FTS5 Volltext-Suche
- `GET /api/v1/vault/note/{path}` — Einzelne Notiz (Frontmatter + Body)

**Neue Scopes (M.1):**
- `vault:read` — Agents koennen Vault lesen (GET /vault/*)
- `vault:write` — Agents koennen direkt in Vault schreiben (M.2+)

**Lifespan-Registrierung:** VaultWatcher und VaultIndex werden in `main.py` Lifespan-Hook gestartet/gestoppt.

### M.2 — Write Foundation (2026-05-14, implementiert)

- `VaultCompactor` + `POST /agent/vault/note` schreibt via Inbox-Envelope
- Alembic 0112: 881 board_memory rows → fresh Markdown mit `id`-Frontmatter (Schema-Gap aus M.1 closed). 884 Phase-7-Legacy archiviert in `~/.mc/vault.phase7-pre-m2-20260515-000723`. 0 conflicts, 0 rejects.
- VaultGit live: Auto-Commit + Push nach jedem Write
- VaultEmbeddings live: Qdrant `memory_vault` Collection via Spark DGX
- `on_moved` Linux-inotify Handler in VaultWatcher (Filesystem-Move-Events)

### M.3 — Agent Rollout (2026-05-14/15, implementiert)

- 8 cli-bridge Docker-Agents bekommen `vault:read+vault:write` Scopes via SQL UPDATE
- `compose_renderer.write_compose_agents()` schreibt `${HOME}/.mc/vault:/vault:rw` Volume-Mount + `AGENT_VAULT_PATH=/vault/agents/{slug}` + `AGENT_VAULT_INBOX=/vault/_inbox` Env-Vars in `docker/docker-compose.agents.yml`
- `tools_md_builder.py` rendert Vault-Section in TOOLS.md aller berechtigter Agents
- `SOUL.md.j2` Update: Vault-Disziplin (eigener Ordner gehört Agent, keine Cross-Writes ohne Inbox)
- `obsidian_export_enabled=False` (Phase-7-Export deaktiviert)

### M.4 — Visual Graph + Voice (2026-05-15, implementiert)

- `GET /vault/graph` Backend: sklearn k-means clustering, wikilink edges, Qdrant similarity-edges (W3-A), Redis heatmap
- WebSocket `/vault/stream` für Live-Updates + `/voice-highlight` Bridge
- Frontend `MemoryGraph2D` (react-force-graph-2d, Obsidian-Stil):
  - `nodeRadiusFromLinkCount` 2-18px sqrt scaling → sichtbare Hub-Hierarchie
  - `charge=-300, linkDistance=25` für Karpathy-Spread
  - `forceX(0)/forceY(0).strength(0.12)` für sphärische Form (siehe Lessons)
  - Filter-aware edge dimming, kein Auto-zoomToFit
- Voice-Worker (xAI Grok via LiveKit): `vault_briefing`, `vault_search`, `vault_write_note` Tools

### M.5 — BoardMemory Deprecation (2026-05-15, implementiert)

- `board_memory` Tabelle in Migration 0112 entwertet — Daten in Vault migriert, Schreibpfad auf Vault-API umgestellt
- `services/auto_memory.py:record_task_completion()` schreibt jetzt als TaskComment statt BoardMemory (W4 audit-trail separation)
- `services/obsidian_export.py` deprecated
- BoardMemory bleibt für Legacy-Lesezugriff erhalten (no breaking change)

### Boss + Voice Rollout (2026-05-15, implementiert)

Die acht Docker-Agents bekamen ihre Scopes via ad-hoc SQL UPDATE während M.3. Boss (host-runtime, native claude CLI) und Voice (host-runtime, xAI Grok worker) wurden dabei übersehen — beide sind nicht Teil der cli-bridge Docker-Compose-Gruppe.

Fix (Commit `c6b6d59e` + `5ab24da5`):
- Alembic 0114: idempotenter Scope-Grant für Boss + Voice
- Boss `agent.env`: `AGENT_VAULT_PATH=$HOME/.mc/vault/agents/boss` + `AGENT_VAULT_INBOX=$HOME/.mc/vault/_inbox` (host-Pfade, da Boss native läuft)
- `tools_md_builder.generate_tools_md(..., runtime="host")` — neue runtime-Variante phrast "host-Pfad `~/.mc/vault/...`" statt "im Container"
- Boss `claude-config/TOOLS.md` regeneriert mit Vault-Section
- Voice: keine TOOLS.md-Regeneration nötig — Voice nutzt kein Claude-CLI sondern xAI Function-Calling über `voice_worker/mc_client.py:vault_*`

**Henry bleibt absichtlich draussen** — OpenClaw Council Gateway-Agent, nicht MC-orchestrierter Worker. Hat keine eigene Identity im MC-Sinn.

## Alternativen

- **Palinode (Apple Notes MCP-basiert):** Verworfen — keine OSS-Lizenz, kein Agent-Schreibzugriff ohne AppleScript, nicht containerisierbar.

- **basic-memory:** Verworfen — AGPL-Lizenz (inkompatibel mit MC-Lizenz-Policy), Single-Writer-Architektur (kein Multi-Agent-Concurrent-Write ohne Konflikte), kein natives Redis-Activity-Tracking.

- **Obsidian-Vault weiter als Read-Only-Spiegel (Phase-7-Status-quo):** Verworfen — loest das grundlegende Problem nicht (Agents koennen ihr Wissen nicht eigenstaendig verwalten). Jede Erkenntnis bleibt an den REST-API-Call-Moment gebunden.

- **Qdrant als primarer Speicher (Vektor-First):** Verworfen — kein menschenlesbarer Wissensgraph, kein Git-History-Audit-Trail, Qdrant ist fail-soft Ergaenzung nicht Ersatz. Vektor-Suche ist Retrieval-Layer ueber dem Vault, nicht der Vault selbst.

- **Git-Repo direkt (ohne SQLite-Index):** Verworfen — FTS5-Suche ueber Markdown-Dateien ohne Index ist O(n) fuer jeden Query. SQLite rebuild ist < 1s fuer typische Vault-Groessen (1k-10k Dateien). llmwiki-Pattern ist battle-tested.

## Konsequenzen

### Positiv

- **Agent Ownership:** Agents koennen ihr Wissen in einem natuerlichen Schreibfluss speichern — kein Kontext-Verlust bei Context-Compaction oder Container-Restart wenn Lessons bereits im Vault stehen
- **Offline-resilient:** Vault ist ein lokales Filesystem — kein PostgreSQL-Ausfall verliert Wissen. Index ist rebuild-bar aus dem Vault
- **Obsidian-kompatibel nativ:** Vault-Layout ist per-Design Obsidian-kompatibel. Keine separate Export-Pipeline mehr noetig (OBS-* Services werden in M.3 deprecated)
- **Git-History:** Jeder Agent-Write wird in `vault.git` committed — vollstaendige Wissenshistorie
- **FTS5-Suche < 50ms:** Lokale SQLite-Suche ist signifikant schneller als PostgreSQL Full-Text bei Vault-Groessen
- **Heatmap via Redis:** `VaultActivity` ermoeglicht "welche Dateien werden am haeufigsten bearbeitet" — Input fuer Lorenz-Attraktoren Wissensgraph in M.4+

### Negativ

- **Migrations-Aufwand:** BoardMemory-Eintraege muessen in M.5 in Vault migriert werden. ~1k-2k Zeilen, automatisierbar via Migration-Script, aber Koordination mit live-System noetig
- **Filesystem-Abhaengigkeit:** Backend-Container muss `~/.mc/vault` gemountet haben. Produktiv bereits der Fall via `${HOME}/.mc` Mount in `docker-compose.yml`
- **Quarantaene-Maintenance:** VaultWatcher quarantaeniert invalide .md-Dateien nach `~/.mc/vault/_quarantine/`. Operator muss Quarantaene gelegentlich leeren (kein Auto-Delete)
- **`watchfiles`-Dependency:** Neue Python-Dependency im Backend. Fail-soft bei watchfiles-Fehler: Index bleibt aktuellem Stand, `rebuild_from_vault()` kann manuell getriggert werden

### Schema-Gap: Phase 7 Export ohne `id`-Feld (resolved in M.2 cutover)

Das E2E-Test (T12, 2026-05-14) hat einen wichtigen Schema-Gap aufgedeckt: Der Phase-7-Obsidian-Export (`obsidian_export.py`) schreibt keine `id`-Felder in das Frontmatter der exportierten Markdown-Dateien.

**Resolution:** Alembic 0112 (`board_memory → vault`) backfillt `id` aus `memory/{board_id}/{entry_id}` in alle neu geschriebenen Markdown-Files. Phase-7-Export-Verzeichnis ist archiviert in `~/.mc/vault.phase7-pre-m2-20260515-000723` (884 stale files), neue Migration schreibt 881 fresh Files mit korrektem Frontmatter. Schema-Gap closed.

### Lessons Learned (2026-05-15)

Drei nicht-offensichtliche Bugs aus der Live-Rollout-Phase, dokumentiert hier weil sie sich beim nächsten Force-Graph- oder Constraint-Tuning sonst wiederholen würden:

1. **`d3-force.forceCenter` hat keine `.strength()` Methode.** Calls auf `forceCenter.strength(0.5)` werden silently ignoriert — kein Error, kein Warning, aber auch kein Effekt. Für sphärische Layouts: `forceX(0).strength(0.12)` + `forceY(0).strength(0.12)` explicit setzen. Siehe Memory-Eintrag `d3-force-center-strength-noop` (operator-lokales Claude-Memory, nicht Teil dieses Repos).

2. **W3-C `related_notes min_length=2` war zu strikt.** Die erste Note in einem neuen Vault-Bereich hat legitimerweise keine Nachbarn. Constraint relaxed auf `min=0`, `max=8` bleibt. `vault_wikilink_backfill` Job verknüpft Orphans retroaktiv über Qdrant similarity + Spark LLM.

3. **`tools_md_builder.py` hardcoded "im Container" Phrasierung.** Host-runtime Agents (Boss) sahen "gemappt auf `/vault/agents/boss/` im Container" obwohl sie nativ laufen. Neuer `runtime` Parameter mit Default `"docker"` adaptet die Phrasierung. Boss bekommt `runtime="host"` → "host-Pfad `~/.mc/vault/agents/boss/`".

### Betroffene Services bei M.5 (BoardMemory-Deprecation)

Bei voller Umstellung werden folgende Services angepasst:
- `services/obsidian_export.py` — deprecated, durch direkten Agent-Write ersetzt
- `services/auto_memory.py` — schreibt in Vault statt `board_memory`
- `services/intelligence.py` — liest Vault statt `board_memory` fuer Lesson-Layer
- `routers/memory.py` + `routers/knowledge.py` — Shims auf Vault-Endpoints
- Frontend `/memory` — neue VaultPage statt BoardMemory-Tabs (M.3)

### NOTICE fuer llmwiki-Pattern-Nutzer

Das SQLite-File `~/.mc/vault/.mc_index.db` ist generator-managed — **niemals manuell editieren**. Bei Index-Korruption: `POST /api/v1/vault/rebuild` (nur Admin) oder `python3 -c "from app.services.vault_index import VaultIndex; ..."` im Backend-Container. Backup via `cp .mc_index.db .mc_index.db.bak` ist ausreichend (kein Qdrant-Backup noetig — Qdrant ist rebuild-bar).

## Referenzen

- **Spec:** `docs/superpowers/specs/2026-05-14-mc-memory-vault-as-source-design.md`
- **Plan:** `docs/superpowers/plans/2026-05-14-vault-memory-m1-read-foundation.md`
- Betroffene Dateien (M.1):
  - `backend/app/services/vault_index.py` — FTS5 SQLite Index
  - `backend/app/services/vault_activity.py` — Redis Heatmap
  - `backend/app/services/vault_git.py` — Git Stub
  - `backend/app/services/vault_embeddings.py` — Embeddings No-op Stub
  - `backend/app/services/vault_watcher.py` — FS Watcher + Quarantaene
  - `backend/app/routers/vault.py` — Read API
  - `backend/app/main.py` — Lifespan-Registrierung
  - `backend/app/config.py` — `vault_path`, `vault_embed_enabled` Settings
  - `backend/tests/test_vault_*.py` — Tests
- Commits (M.1, 2026-05-14):
  - `8226e8ba` — docs(spec): MC Memory Vault as Source of Truth
  - `bbf03fe1` — docs(plan): M.1 Read Foundation implementation plan (13 tasks)
  - `0011890e` — feat(vault): add deps, vault_path config, vault scopes, NOTICE
  - `ffafc227` — feat(vault): SQLite FTS5 index with upsert (llmwiki pattern)
  - `88747d65` — feat(vault): DGX → Qdrant embeddings adapter with fail-soft
  - `1d02eb84` — feat(vault): Redis sortedset activity tracker for heatmap
  - `4817f0a5` — feat(vault): rebuild_from_vault() — full re-index walking md files
  - `3af19035` — feat(vault): watchdog-based VaultWatcher with validation + quarantine
  - `9b6a48b0` — feat(vault): read API routes — /notes, /search, /note/{path}
  - `4d17f435` — feat(vault): wire vault services into FastAPI lifespan
  - `fe3ea9af` — test(vault): e2e integration test against real vault subset
- Commits (M.2-M.4, 2026-05-14/15):
  - Alembic 0112 — `board_memory → vault` Cutover-Migration (881 rows, id-Backfill)
  - `81101319` — fix(vault): auto-rebuild FTS5 index after destructive schema migration
  - `a83f3d10` — fix(vault): Qdrant API + TS errors after live orchestrator run
  - `89f0692a` — fix(vault): index frontmatter title + stable graphData memo
  - `e7889fe2` — fix(vault): drop similarity edges referencing W1-archived notes
  - `8c6fcfc5` — fix(memory-graph): forceX/forceY for spherical Obsidian shape
  - `cd7b5305` — feat(memory-graph): true Obsidian-style constellation layout
  - `1fc29f8f` — revert(memory-graph): restore c84537e4 visual baseline that the operator approved
- Commits (Boss + Voice Rollout, 2026-05-15):
  - `c6b6d59e` — fix(vault): grant Boss access + relax W3-C related_notes constraint
  - `5ab24da5` — feat(vault): alembic 0114 — codify Boss + Voice vault scopes
- Merge: `0b35ed83` — Merge feature/vault-memory-foundation into main (102 commits)
- Verwandte ADRs:
  - [ADR-004](004-board-memory-unified.md) — BoardMemory als Single Knowledge-Table (wird durch dieses ADR in M.5 superseded)
  - [ADR-022](022-mc-home-workspace-layout.md) — `~/.mc/` Home Layout (definiert `~/.mc/vault/` Basis-Pfad)
  - [ADR-006](006-jinja2-template-source-of-truth.md) — Template als SoT (Pattern-Vorlage fuer Vault-as-SoT)
- Externe Quellen:
  - Karpathy Blog: Plain-text wiki als long-term memory fuer LLMs
  - llmwiki: SQLite FTS5 als rebuild-barer Index ueber Markdown-Corpus
