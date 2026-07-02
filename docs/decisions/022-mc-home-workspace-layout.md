# ADR-022 — `~/.mc/` Home + Standardized Workspace Layout

**Status:** Accepted
**Datum:** 2026-04-21
**Scope:** Infra/Runtime · Backend/Provisioning · Agent Protocol
**Supersedes:** —

## Kontext

Nach der Phase-2-Migration (ADR-020/021) war die Workspace-Konfiguration
in drei inkonsistenten Zuständen gleichzeitig:

1. **`agent.workspace_path` DB-Feld** hatte einen Mix aus Host-Pfaden
   (`/Users/YOUR_USER/.openclaw/workspace-rex`) und Container-Pfaden
   (`/workspace/Projects/`) — backend und Agent lasen das gleiche Feld,
   aber aus unterschiedlichen Perspektiven.
2. **Docker Volume-Mounts** waren ungleich verteilt: FreeCode / Tester /
   Researcher / Sparky hatten `~/Workspace:/workspace` gemountet, Rex /
   Davinci / Shakespeare / Deployer hatten gar keinen Workspace-Mount.
3. **`.mc-scratch/` + `.mc-deliverables/{task_id}/` Konvention** aus PR #48
   setzte voraus dass ein schreibbarer Workspace existiert — ist aber
   vom Container aus nicht erreichbar wenn der Workspace-Path des
   Backends auf den Host (`~/.openclaw/workspace-<slug>`) zeigt und
   nicht im Container gemountet ist.

Zusätzlich: das ganze System heisst seit ADR-014/019 "Mission Control"
— `~/.openclaw/` ist ein historisches Relikt aus der openclaw-
Gateway-Ära und bietet keine klare Grenze zwischen Gateway-Dateien und
MC-eigenen Dateien.

## Entscheidung

Neuer MC-Home unter **`~/.mc/`**, strikt getrennt von `~/.openclaw/`.
Klare Subdir-Struktur, einheitliche Host/Container-Pfad-Übersetzung.

### Filesystem-Layout (Host)

```
~/.mc/
├── agents/<slug>/           # Agent-Config: SOUL.md, HEARTBEAT.md, agent.env,
│   │                        # claude-config/ — was vorher in ~/.openclaw/agents/
│   └── claude-config/
├── workspaces/<slug>/       # Per-Agent Arbeitsplatz (rw mount → /workspace)
│   ├── projects/<project>/  # Git-Clone per Projekt, persistent
│   │   └── .worktrees/
│   │       └── <task-slug>/ # Per-Task Worktree (git worktree add)
│   │           ├── .mc-scratch/         # gitignored
│   │           └── .mc-deliverables/<task_id>/  # committed
│   └── adhoc/<task-slug>/   # Tasks ohne project_id
├── deliverables/<slug>/     # Was vorher ~/.mc-deliverables/<slug>/
├── mcp-servers/             # MCP-Server-Definitions
├── plugins/                 # Shared CLI-Plugins (claude-code plugins)
├── skills/                  # Custom Skills (mc-debug, mc-tdd, …)
├── backups/                 # DB + Config Backups (künftig)
└── logs/                    # Zentrale Log-Ablage (optional)
```

### Docker-Mount-Standard (pro cli-bridge Agent)

```yaml
volumes:
  - ${HOME}/.mc/agents/<slug>/claude-config:/home/agent/.claude
  - ${HOME}/.mc/mcp-servers:/mc-servers:ro
  - ${HOME}/.mc/workspaces/<slug>:/workspace                  # rw, Arbeitsplatz
  - ${HOME}/Workspace/Projects:/workspace-ref:ro              # ro, Code des Operators
  - ${HOME}/.mc/deliverables/<slug>:/deliverables
```

### Per-Agent `/workspace-ref:ro` Policy

Read-only Mount auf `~/Workspace/Projects/` (nicht `~/Workspace/` root!)
gibt es nur für Agents die Code-Zugriff brauchen:

- **Bekommen es:** Rex, FreeCode, Sparky, Tester, Deployer, Researcher
- **Bekommen es NICHT:** Shakespeare (Writer), Davinci (Visual) —
  Security-Reduktion, sie brauchen keinen Code-Zugriff.

Mount ist auf `Projects/` beschränkt, nicht auf `~/Workspace/` root, damit
private Unterordner (`.ssh`, `Library`, Downloads) nicht leaken. `.env`-Files
in Project-Roots werden separat im Agent-SOUL als *"nie lesen"* markiert.

### Host/Container Pfad-Übersetzung

`agent.workspace_path` speichert immer den **Host-Pfad** (was Backend
sieht). Dispatch-Templates und SOUL-Rendering übersetzen zum
Container-Pfad via `_container_workspace_path(host_path, agent)`:

- `~/.mc/workspaces/<slug>/…` → `/workspace/…` (cli-bridge agents)
- `~/.openclaw/workspace-<slug>/…` → `/workspace/…` (legacy fallback)
- Boss (host runtime) + Henry (openclaw) sehen weiterhin Host-Pfade

### Boss + Henry Besonderheiten

- **Boss:** `workspace_path = ~/Workspace` (host runtime, kein Mount,
  hat direkten Filesystem-Zugriff).
- **Henry:** `workspace_path = ~/Workspace/Projects` — Henry ist
  Messenger, braucht aber Lookup-Fähigkeit wenn der Operator ihn bittet kurz
  was im Code nachzuschauen.

### Backward-Compatibility via Symlink

Statt 60+ Code-Files auf neue Pfade zu refactoren: Symlink
`~/.openclaw/agents → ~/.mc/agents`, `~/.openclaw/mcp-servers →
~/.mc/mcp-servers` usw. Alte Code-Pfade folgen transparent. Progressive
Code-Migration in späteren PRs.

## Alternativen

- **Bleiben bei `~/.openclaw/`.** Verworfen — Name-Gap zwischen System
  ("Mission Control") und Home-Dir verwirrt Future-Me. Sauberer jetzt.
- **`~/.mission-control/` statt `~/.mc/`.** Verworfen — zu lang für
  CLI-Daily-Use (`cd ~/.mc/agents/rex` schlägt Autocomplete mit einer
  Eingabe, `~/.mission-control/` braucht mehrfach Tab).
- **Shared `/repos/` für alle Agents statt per-Agent Clone.** Verworfen
  — Git-Worktrees auf shared `.git/` haben Race-Conditions bei 8
  parallelen Agents. Per-Agent Clone (300 MB × N Projekte pro Agent)
  ist Disk-freundlich genug.
- **Alle Agents bekommen `~/Workspace/`:/workspace-ref:ro**. Verworfen —
  Shakespeare/Davinci haben echt keinen Code-Lese-Bedarf; ro-Mount auf
  die privaten Ordner des Operators ist unnötige Angriffsfläche.

## Konsequenzen

### Positiv

- **Ein Host-Dir pro System:** `~/.mc/` = MC; `~/.openclaw/` bleibt nur
  für echte openclaw-Gateway-Files (aktuell faktisch leer).
- **Einheitliche Mount-Struktur:** Alle 7 Docker-Agents haben
  symmetrische Mounts. Neue Agents werden 1:1 nach Template erstellt.
- **Klare Host/Container-Trennung:** Backend arbeitet mit Host-Pfaden,
  Agent sieht Container-Pfade. Übersetzungs-Layer ist zentral in
  `_container_workspace_path()`.
- **Sauberes `.mc-scratch/` + `.mc-deliverables/` Flow:** Workspace ist
  garantiert schreibbar für den Agent. Der Path der in Dispatch-Prompts
  steht ist der Path den der Agent wirklich sieht.
- **Security-Reduktion:** Shakespeare/Davinci ohne Code-Zugriff. Mounts
  auf `~/Workspace/Projects/` (nicht root) — private Dirs geschützt.
- **Backup-Zentralisierung:** `~/.mc/backups/` als single-place für DB
  und Config-Archive (später).

### Negativ

- **Migration-Aufwand einmalig:** rsync von 18 GB (~15 min),
  Docker-Recreate, Alembic-Migration 0087. Aber mit Backup +
  Symlink-Fallback reversibel.
- **`/workspace-ref:ro` Security-Nachdenken:** Agent sieht `.env` falls
  der Operator welche in Repo-Roots hat. Policy in Agent-SOUL: "Nie `.env`
  lesen". Nicht technisch durchgesetzt (ro-Mount verhindert write, aber
  Read ist by design). Ist akzeptiertes Restrisiko.
- **Code-Pfad-Referenzen in 60+ Files sind semantic-veraltet** (referenzieren
  `~/.openclaw/`). Funktional OK dank Symlink, aber für Lesbarkeit
  sollten progressive PRs die Pfade zu `~/.mc/` aktualisieren.

## Rollout

1. Pre-Backup: `./backup.sh` (DB + `~/.openclaw/` Tarball) + rsync-Mirror
   `~/mc-preworkspace-migration-YYYY-MM-DD/`.
2. Docker-Agents stoppen (`docker compose -f docker/docker-compose.agents.yml down`).
3. Dateisystem: `rsync -a ~/.openclaw/ ~/.mc/`, dann `rsync -a ~/.mc-deliverables/ ~/.mc/deliverables/`.
4. Agent-Workspace-Dirs: `mv ~/.openclaw/workspace-<slug> ~/.mc/workspaces/<slug>` (per Agent).
5. Symlinks für backward-compat: `ln -s ~/.mc/agents ~/.openclaw/agents.link` (nicht
   die alten Dirs überschreiben, nur `.link`-Alias erstellen).
6. Alembic upgrade head (bringt Migration 0087 → `agent.workspace_path`
   auf `~/.mc/workspaces/<slug>`).
7. Docker-compose.agents.yml nutzt bereits die neue Struktur → Agents
   recreate (`docker compose -f docker/docker-compose.agents.yml up -d
   --force-recreate`).
8. Smoke-Test: `docker exec mc-agent-rex bash -c "ls /workspace && mc --version"`.

## Referenzen

- Key Files:
  - `backend/alembic/versions/0087_workspace_layout_mc_home.py`
  - `backend/app/services/dispatch.py` → `_container_workspace_path()`
  - `docker/docker-compose.agents.yml` — neue Mount-Struktur
- Verwandte ADRs:
  - ADR-006 — Jinja2-Templates als SSoT (Backend → Templates → Files)
  - ADR-013 — settings.json als echte Kopie im Docker-Mount
  - ADR-019 — Claude Fleet Hybrid
  - ADR-020 — Harness Phase 2 (mc CLI + Progress SSoT)
