# ADR-024 — Claude-Process Recycling im Docker-Agent-Container

**Status:** Accepted
**Datum:** 2026-04-26
**Scope:** Infra/Runtime · Backend/Provisioning · Container Lifecycle
**Supersedes:** —
**Related:** ADR-013 (Docker-V2 tmux-Layout), ADR-019 (Claude Fleet Hybrid), ADR-022 (~/.mc Home Layout)

## Kontext

Phase 2 Memory-Baseline (MEM-02, `.planning/notes/memory-baseline.md`) hat den
`claude` Binary als dominante 24h-RAM-Wachstumsquelle in der Docker-Agent-Flotte
identifiziert: Σ +3380 MB ueber 8 mc-agent-* Container, Peak 906 MB auf
mc-agent-researcher. bun = 0 MB Wachstum, node = 175 MB (≤5% von claude).
Container-Restart-Frequenz wuerde laufende Tasks reissen → wir brauchen einen
**intra-container** Mechanismus.

Phase 1 (REL-01..07) + Phase 2 (MEM-02..05) haben die Voraussetzungen
geschaffen: ACK-Handshake ueberlebt Container-Restart-Faelle, Jinja-Cache
+ Qdrant-Index + Intelligence-Interval haben die anderen RAM-Verbraucher
auf Floor gedrueckt — verbleibender Wachstum ist eindeutig dem `claude`
Binary zuzuordnen.

## Entscheidung

Bash-Watchdog `recycler.sh` lebt als tmux Window 2 im Container. Loop alle 60s:

- **Idle-Trigger:** mtime des `/home/agent/.claude/last-task.marker` (von
  poll.sh nach `heartbeat "working"` geschrieben) ≥ 15 min →
  `tmux respawn-pane -t {session}:0 -k`.
- **Threshold-Trigger (Safety-Net):** claude RSS > 1500 MB → respawn
  unabhaengig von Idle-State.
- **Debounce:** minimaler Abstand 5 min zwischen Recycles.

**Window-Topologie** (erweitert ADR-013):

- Window 0: `claude` (start-claude.sh loop) — Recycle-Target
- Window 1: `poll.sh` (Task-Polling) — UNANGETASTET
- Window 2: `recycler.sh` (NEU) — Watchdog

**Zwei-Tier Kill-Switch** (mirror MEM-05 Phase 1 D-09..D-11):

- **Global:** `AGENT_RECYCLER_ENABLED` env-var in
  `docker/docker-compose.agents.yml` (Default: `true`).
- **Per-agent:** `agents.recycler_enabled` (BOOL nullable). NULL = follow
  global; True/False = explicit per-agent override.

Backend rendert effektiven Wert beim sync-config in `agent.env` UND als
Bootstrap-Token. Container liest einmal beim Start, kein Runtime-Round-Trip.
Recycler-Script no-ops via `exec sleep infinity` wenn deaktiviert.

## Alternativen

- **Container-Restart automatisieren** (`docker restart`-loop) → verworfen.
  Reisst laufende Tasks. ADR-022 + Phase-1 ACK-Handshake bauen Container-
  Stabilitaet darauf, dass laufende Tasks Restarts ueberleben — das gilt fuer
  *einzelne* Restarts, nicht 24x/Tag.
- **`tmux send-keys C-c C-c`** statt `respawn-pane` → verworfen. claude kann
  mid-MCP-call SIGINT ignorieren oder stallen. `respawn-pane -k` SIGKILLt
  decisive — der einzig verlorene State ist die in-memory Session, genau
  das was raus soll. Live verifiziert auf mc-agent-rex (PID 39 → 99794,
  Container blieb up, bash-loop lief weiter).
- **Backend-Round-Trip statt Marker-File** → verworfen. Koppelt Recycler an
  Backend-Verfuegbarkeit. Falls Backend down ist, soll der Recycler erst
  recht weiter laufen.
- **Python-Script statt Bash** → verworfen. Python-Interpreter ~30-40 MB
  resident pro Container. Wir reduzieren RAM, nicht hinzufuegen.
- **Sidecar Docker container fuer Watchdog** → verworfen. Adds compose
  complexity, mount sharing fuer `/proc/{pid}/`, network for tmux RPC —
  no upside given the in-container model is trivial.
- **Adaptive RSS-Threshold** (auto-tune basierend auf 7-Tage-Soak-Daten) →
  deferred. Premature optimization; static 1500 MB threshold first, manuell
  tunen nach erstem Soak.

## Konsequenzen

### Positiv

- Σ Container-RAM-Wachstum ueber 7-Tage-Soak: 3380 MB → ≤ 700 MB (Ziel).
- Kein Task-Verlust (Window 1 poll.sh laeuft durch, Window 0 startet leer
  und bekommt naechsten Task via re-dispatch).
- Two-tier Kill-Switch erlaubt surgical disable per Agent ohne Redeploy.
- Wiederverwendung existierender Infrastruktur (tmux, pgrep, ps, stat) —
  keine neuen Container-Deps.
- Observability via `docker logs` (selber Channel wie `[entrypoint]` und
  `[watchdog]` Lines).

### Negativ

- Idle-15min-Window verschiebt Cold-Start von ~200ms (warmer Prozess) auf
  ~3-5s (frischer claude + SOUL.md re-load). Akzeptabel — passt eh in den
  ACK-Timeout (15 min fuer cli-bridge, ADR-018 / Phase 1).
- Fortlaufende Recycle-Events tragen Logs an (geringe Verbose, ~1 Zeile/h
  typisch).
- Sparky (openclaude binary) ist via `pgrep -x claude` exact-match Recycler-
  no-op. Beabsichtigt — Sparky hat anderen Memory-Footprint und sollte
  separat untersucht werden falls noetig.

## Soak-Validation

### Vor (Phase 2 Baseline, 24h Fenster bis 2026-04-25 20:23 UTC)

Quelle: `.planning/notes/memory-baseline.md` (Phase 2 MEM-02 Verdict, generiert
von `tools/per-process-snapshot.py` vor Recycler-Deployment).

- **Σ claude growth (24h restart-aware):** +3380 MB ueber 8 mc-agent-* Container
- **Σ claude peak:** 4050 MB
- **Peak RSS:** 906 MB auf mc-agent-researcher
- **Σ bun growth:** +0 MB (kein Beitrag)
- **Σ node growth:** +175 MB (~5% von claude — Minor)

Per-Container claude restart-aware growth (Phase 2 baseline):

| Container | claude growth | claude peak |
|-----------|--------------:|------------:|
| mc-agent-davinci | +495 MB | 432 MB |
| mc-agent-deployer | +293 MB | 438 MB |
| mc-agent-freecode | +451 MB | 603 MB |
| mc-agent-researcher | +789 MB | 906 MB |
| mc-agent-rex | +601 MB | 415 MB |
| mc-agent-shakespeare | +297 MB | 426 MB |
| mc-agent-sparky | +86 MB | 411 MB |
| mc-agent-tester | +368 MB | 419 MB |
| **Σ** | **+3380 MB** | **4050 MB** |

### Day-1 (post-deploy, 2026-04-26 12:03 UTC, T+2h nach Recycler-Live)

Quelle: `.planning/notes/memory-baseline-day1-postdeploy.md`. Hinweis: das
24h-Fenster enthaelt bei T+2h noch ueberwiegend pre-deploy Daten — diese
Snapshot dient als **Brueckenmessung** (Recycler-Aktivitaetsnachweis nach
container recreate), nicht als Soak-Verdict. Das echte Vergleichsmoment ist
T+7d (siehe Naechster Abschnitt).

- **Σ claude growth (24h restart-aware):** +2207 MB ueber 7 mc-agent-* Container (tester fehlt — keine CSV samples seit recreate)
- **Σ claude peak:** 3137 MB
- **Peak RSS:** 436 MB auf mc-agent-freecode (deutlich unter 906 MB Phase-2 Peak)
- **Recycler-Aktivitaet seit soak start (2026-04-26T11:50:04Z):**
  - 8/8 Container haben `[agent-recycler ...] starting` log line
  - mc-agent-rex: 1 recycle event (vom Smoke-Test in Plan 03-06)
  - 7/8 Container: 0 recycles bisher (zu frueh — Idle-Trigger braucht ≥15 min Idle, RSS-Trigger braucht >1500 MB)

### Nach (7-Tage-Soak, T+7d ≥ 2026-05-03T11:50:04Z)

`[TO BE POPULATED 2026-05-03+ — Plan 03-07 Tasks 4-6 fuellen diese Sektion mit
post-soak Σ growth, recycle-event count per container, und finale Verdict-Zeile.]`

Plan 03-07 Sign-off Workflow:

1. Re-run `tools/per-process-snapshot.py` (ueberschreibt
   `.planning/notes/memory-baseline.md` mit T+7d Daten)
2. Vergleich Verdict-Section: Phase 2 baseline (oben) vs T+7d output
3. Per-container recycle-event count via
   `docker logs --since 2026-04-26T11:50:04Z <container> | grep -c "agent-recycler.*recycled"`
4. Der Operator verifiziert D-17 Acceptance Condition (a) ODER (b)

### Erfolgs-Kriterium (D-17)

Σ growth ≤ ±10% baseline (3380 ± 338 MB) ODER ≥3 Container mit flachem
Trend ueber den 7-Tage-Window. `tools/per-process-snapshot.py` ueberschreibt
`.planning/notes/memory-baseline.md` — Vergleich Verdict-Section vorher/nachher.

## Rollback

### Globaler Kill-Switch (alle 8 mc-agent-* Container)

**Hintergrund:** Die compose-Definition nutzt `${AGENT_RECYCLER_ENABLED:-true}`
als Default — d.h. wenn die Variable NICHT in der Shell-Umgebung oder einer
`--env-file` gesetzt ist, wird sie automatisch auf `true` gesetzt. Fuer den
Rollback muss sie explizit auf `false` gesetzt werden.

Schritt 1: `AGENT_RECYCLER_ENABLED=false` setzen. Drei aequivalente Wege
(in Reihenfolge der Empfehlung):

```bash
# Variante A (empfohlen, persistent): Zeile zu docker/.env.agents hinzufuegen
echo "AGENT_RECYCLER_ENABLED=false" >> docker/.env.agents

# Variante B (persistent, alternativ): Zeile zu .env hinzufuegen
echo "AGENT_RECYCLER_ENABLED=false" >> .env

# Variante C (one-shot, nicht persistent): inline beim compose-Aufruf
AGENT_RECYCLER_ENABLED=false docker compose ... up -d --force-recreate ...
```

Schritt 2: Force-recreate aller Agent-Container mit der kanonischen
Multi-File-Compose-Invocation (Caveat 2 aus Plan 03-05):

```bash
docker compose \
  -f docker-compose.yml \
  -f docker/docker-compose.agents.yml \
  --env-file .env \
  --env-file docker/.env.agents \
  up -d --force-recreate \
  mc-agent-rex mc-agent-freecode mc-agent-tester mc-agent-deployer \
  mc-agent-researcher mc-agent-shakespeare mc-agent-davinci mc-agent-sparky
```

Schritt 3: Verifizieren dass Recycler im disabled-Pfad ist:

```bash
# Pro Container: Recycler logged "disabled" line + ist in sleep infinity
for c in $(docker ps --filter 'name=mc-agent-' --format '{{.Names}}'); do
  docker logs --tail 30 "$c" 2>&1 | grep -E "agent-recycler.*disabled" \
    && echo "$c: ✓ disabled"
done

# Pro Container: claude-Prozess existiert weiter (nur recycler.sh ist no-op)
for c in $(docker ps --filter 'name=mc-agent-' --format '{{.Names}}'); do
  pid=$(docker exec "$c" pgrep -x claude || echo "none")
  echo "$c: claude PID = $pid"
done
```

Recycler stoppt (springt direkt in `exec sleep infinity` beim naechsten
Container-Start). claude-Prozesse akkumulieren wie vor Phase 3 — keine
Regression, nur der Status quo ante.

### Per-agent Override (chirurgisch, ohne Redeploy)

```sql
-- Recycler fuer einzelnen Agent deaktivieren:
UPDATE agents SET recycler_enabled = false WHERE name = 'rex';
```

```bash
# Backend sync-config rendert agent.env neu (Plan 03-04 env-render Pfad);
# naechster Container-Start (oder docker restart) liest den neuen Wert:
curl -s -X POST -H "Authorization: Bearer $ADMIN_JWT" \
  http://localhost:8000/api/v1/agents/$AGENT_ID/sync-config
docker compose -f docker/docker-compose.agents.yml restart mc-agent-rex
```

### Verifikations-Sequenz (synthetisch — bricht recycler NICHT ab)

Sicherheits-Test des Rollback-Pfads ohne Recycler tatsaechlich abzuschalten
(z.B. waehrend Soak-Window):

```bash
# 1. Compose-Datei prueft alle 8 Services haben AGENT_RECYCLER_ENABLED:
docker compose \
  -f docker-compose.yml \
  -f docker/docker-compose.agents.yml \
  --env-file .env \
  --env-file docker/.env.agents \
  config | grep -c "AGENT_RECYCLER_ENABLED"
# Erwartet: 8 (eine Zeile pro mc-agent-* Service)

# 2. OS-level env-var ist in jedem Container sichtbar:
for c in $(docker ps --filter 'name=mc-agent-' --format '{{.Names}}'); do
  val=$(docker exec "$c" sh -c 'echo $AGENT_RECYCLER_ENABLED')
  echo "$c: AGENT_RECYCLER_ENABLED=$val"
done
# Erwartet: 8 Zeilen mit =true (waehrend Recycler aktiv ist)

# 3. claude-config/.env hat den geschriebenen Wert (Plan 03-04 env-render):
for c in $(docker ps --filter 'name=mc-agent-' --format '{{.Names}}'); do
  agent=${c#mc-agent-}
  if [ -f ~/.openclaw/agents/$agent/claude-config/.env ]; then
    val=$(grep AGENT_RECYCLER_ENABLED ~/.openclaw/agents/$agent/claude-config/.env || echo "missing")
    echo "$c: .env line = $val"
  fi
done
```

Wenn alle drei Checks ✓ sind, ist der Env-Var-Pfad fuer den Kill-Switch
verifiziert reachable — der Rollback wird funktionieren ohne dass man ihn
real durchfuehren muss.

## Referenzen

- **Key Files (Phase 3 Implementation):**
  - `docker/mc-agent-base/recycler.sh` (NEW, Plan 03-01 skeleton, Plan 03-05 body)
  - `docker/mc-claude-agent/recycler.sh` (NEW, Plan 03-01 skeleton, Plan 03-05 body)
  - `backend/app/services/recycler_config.py:get_effective_recycler_enabled` (NEW, Plan 03-02)
  - `backend/alembic/versions/0090_agent_recycler_enabled.py` (NEW, Plan 03-03)
  - `backend/app/config.py:agent_recycler_enabled` (MOD, Plan 03-02)
  - `backend/app/models/agent.py:recycler_enabled` (MOD, Plan 03-03)
  - `backend/app/services/docker_agent_sync.py` (MOD, Plan 03-04 — env-line)
  - `backend/app/routers/internal.py:agent_bootstrap` (MOD, Plan 03-04)
  - `docker/mc-agent-base/entrypoint.sh` (MOD, Plan 03-05 — Window 2 spawn)
  - `docker/mc-claude-agent/entrypoint.sh` (MOD, Plan 03-05 — mirror)
  - `docker/mc-agent-base/poll.sh` (MOD, Plan 03-05 — touch marker after heartbeat)
  - `docker/docker-compose.agents.yml` (MOD, Plan 03-05 — env-var on services)
- **Verwandte ADRs:** ADR-013 (Docker-V2 tmux), ADR-019 (Claude Fleet),
  ADR-022 (~/.mc Layout)
- **Phase-Doc:** `.planning/phases/03-memory-leak-root-cause-fix/`
- **Datenquellen:**
  - `~/Library/Logs/openclaw-memlog/snapshots-*.csv` (sampler 5-min Interval,
    8+ Tage Daten Stand 2026-04-26)
  - `tools/per-process-snapshot.py` (re-runnable, ueberschreibt
    `.planning/notes/memory-baseline.md`)
- **CONTEXT-Decisions:** D-01..D-18 in
  `.planning/phases/03-memory-leak-root-cause-fix/03-CONTEXT.md`
