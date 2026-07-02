# ADR-026 — Context Management & Auto-Recovery (CTX + REC merger)

- Status: Accepted
- Date: 2026-04-27
- Phase: 6 (v0.5 Hardening)
- Plans: 06-00..06-07
- Supersedes: none
- Superseded by: none

## Kontext

v0.5 Phase 6 vereinigt Context Management (CTX-01..03) und Auto-Recovery
(REC-01..03) in einer Phase, weil CTX-01 (per-agent Token-Tracking) das Signal
liefert, das REC braucht um "stuck wegen Context-Overflow" von "stuck wegen
Process-Crash" zu unterscheiden.

Bestehender Zustand: `_reset_overflowed_sessions`
(`backend/app/services/watchdog/session_monitor.py:342-409`) ruft
`rpc.sessions_reset()` direkt auf — verletzt die CLAUDE.md "Absolute Verbote":
Gateway-Reset ohne Structured Recovery Recap als erste Nachricht. Phase 1 hat
dafuer `runtime_context.get_session_context_for_runtime` geliefert; CTX-02
muss es endlich nutzen.

Stale-Detection (`task_runner._check_stale_in_progress`,
`backend/app/services/task_runner.py:523-694`) eskaliert direkt an den
Operator statt tiered Recovery zu fahren — das fuehrt zu "MC Discord-Spam ohne erst probiert
zu haben den Agent wiederzubeleben".

## Entscheidung

Phase 6 ist gemerged-bereit auf `gsd/phase-6-context-management-auto-recovery`
(noch nicht gepusht — wartet auf den manuellen Smoke-Test des Operators). Alle 6 v1
Requirements (CTX-01..03 + REC-01..03) sind in REQUIREMENTS.md als Complete
markiert. Die folgenden 6 Bausteine wurden umgesetzt:

1. **CTX-01** — Docker self-report ueber `poll.sh` tmux-statusline Scrape;
   Heartbeat-Endpoint nimmt `context_pct` an, derived
   `context_tokens = round(pct/100 * context_max)`. Validation: `0 <= pct <= 100`.

2. **CTX-02** — `_compact_overflowed_sessions` ersetzt
   `_reset_overflowed_sessions` an einer einzigen Stelle. Threshold
   80% → 85%. Flow: Checkpoint-Instruktion → 60s Wait → Reset via
   `get_session_context_for_runtime` (Absolute Verbote enforcement).

3. **CTX-03** — `agent.compaction` Event mit strukturiertem Detail-Dict
   (`context_pct`, `total_tokens`, `context_limit`, `checkpoint_summary_received`);
   existing AgentCard Bar bleibt unangetastet (Threshold-Contracts in
   `lib/utils.contextColor` sind separate Concerns — siehe 06-UI-SPEC.md).

4. **REC-01** — Tiered Recovery in
   `task_runner._check_stale_in_progress`:
   - Tier 1: Heartbeat Probe `asyncio.timeout(10)`
   - Tier 2: Per-Runtime Restart (Docker / Host / skip)
   - Tier 3: Resume mit Structured Recovery Recap (Absolute Verbote)
   - Tier 4: Operator-Notification via `severity='error'` `emit_event`

5. **REC-02** — Per-Runtime Branching (existierende
   `restart_docker_agent_container` + `_host_agent_lifecycle("restart")`
   werden aufgerufen; cli-bridge / openclaw skippen Tier 2).

6. **REC-03** — Activity Events ALS Audit-Log (kein neues DB-Table) — 4 neue
   Event-Types: `agent.compaction`, `agent.recovery_started`,
   `agent.recovery_tier_complete`, `agent.recovery_failed`. severity='error'
   triggert Discord automatisch (existierender Pfad in `activity.emit_event`).

## Alternativen

Die folgenden Optionen wurden während der Phase-6-Discuss-Phase betrachtet
und verworfen:

1. **CTX und REC als zwei separate Phasen** — abgelehnt, weil CTX-01
   (Token-Tracking) das Signal liefert, das REC braucht um "stuck wegen
   Context-Overflow" von "stuck wegen Process-Crash" zu unterscheiden. Eine
   Aufteilung würde das Tier-1 Heartbeat-Probe von REC-01 auf einen blinden
   Restart erweitern (kein Context-Signal verfügbar) und CTX-02 könnte ohne
   das tiered Recovery-Fallback nicht sicher 85% triggern.

2. **Eigenes `recovery_history` Table statt Activity Events** — abgelehnt
   per D-23/D-24 in 06-CONTEXT.md. Activity Events erfüllen alle drei
   Audit-Anforderungen (who/what/when), fließen automatisch in
   `/activity/stream` SSE für UI-Sichtbarkeit, und triggern
   `severity='error'` → Discord ohne weiteren Code. Ein dediziertes Table
   wäre eine ungenutzte Parallelstruktur.

3. **Tier-Gate auf Worker statt auf Watchdog** — abgelehnt, weil Worker
   selbst beim Stuck-Sein nicht zuverlässig "ich bin stuck" reporten können
   (genau das ist die Failure Mode). Der Watchdog muss extern beobachten
   und entscheiden.

4. **Tier-2 als blocking statt `asyncio.to_thread`** — abgelehnt; der
   docker-restart hat eine 20s Subprocess-Timeout-Grenze, blocking würde
   den Watchdog-Tick-Loop für die Dauer einfrieren und andere Stale-Checks
   verzögern.

5. **Compaction durchgehend (kein Threshold)** — abgelehnt, weil
   Compaction nicht-trivialen Token-Overhead verursacht (Checkpoint-Prompt
   + 60s Wait). 85% ist der gewählte Trade-off zwischen "spätestmöglich
   reagieren" (Token-Budget effizient nutzen) und "rechtzeitig agieren"
   (vor 100% Stuck-Zustand).

## Konsequenzen

**Test-Count Delta (gemessen am Plan 06-07 Sign-off):**

| Suite | Plan 05-07 Baseline | Plan 06-07 gemessen | Delta |
|---|---|---|---|
| Backend `pytest -q` | 1330 passed / 1 skipped / 0 xfailed / 0 failed | **1348 passed / 1 skipped / 0 xfailed / 0 failed** | **+18 passed** |
| Frontend-v2 `npm run test:run` | 10 passed / 4 files | **14 passed / 5 files** | **+4 passed, +1 file** |
| Phase 1 race tests `test_dispatch_race.py` | 3/3 green | **3/3 green** | unchanged ✓ |

Erwartet waren +14 (5 tiered_recovery + 4 compaction + 3 heartbeat_context_pct
+ 2 redis_keys); gemessen sind +18 — Plan 06-02 hat 2 Bootstrap-Tests
mitgeliefert (über die 3 Heartbeat-Stubs hinaus) und Plan 06-04 hat 2
Compaction-Extras (counter increment + kill-switch Pfad) zusätzlich zu den
4 ursprünglichen Stub-Flips. Frontend +4 = Plan 06-06 (4 ActivityFeed
event-type Cases).

**Module-Diffs (gemessen via `git diff --stat 649c0336 HEAD`, dem Phase-6-Basis-Commit):**

| Datei | Lines | Änderung |
|---|---|---|
| `backend/app/services/task_runner.py` | +216 / -33 | Plan 06-05 — `_run_tiered_recovery` (176 Zeilen inkl. Docstring) + Wiring in `_check_stale_in_progress` |
| `backend/app/services/watchdog/session_monitor.py` | +167 (1 neue Methode) | Plan 06-04 — `_compact_overflowed_sessions` + DEPRECATED-Marker auf `_reset_overflowed_sessions` + 3 Klassenkonstanten |
| `backend/app/redis_client.py` | +14 (2 neue Keys) | Plan 06-01 — `compaction_lock` + `recovery_inprogress` static methods |
| `backend/app/routers/agents.py` | +10 / -0 | Plan 06-02 — `Field` Import, `context_pct` Field auf AgentHeartbeatPayload, Handler write path |
| `backend/app/routers/internal.py` | +5 / -0 | Plan 06-02 — `tokens["CONTEXT_MAX"]` Bootstrap-Key |
| `backend/app/config.py` | +7 / -0 | Plan 06-04 — `context_compaction_enabled: bool = True` Kill-Switch + Kommentar |
| `frontend-v2/src/components/shared/ActivityFeed.tsx` | +7 (4 neue Map-Einträge + 3 Zeilen Kommentar) | Plan 06-06 — `eventTypeToStatus` Map: `agent.compaction` + 3× `agent.recovery_*` |

Gesamt: **+393 insertions / -33 deletions across 7 files** (Phase-6
Implementation, ohne Tests). Container-side
(`docker/shared/poll.sh` + 2× `entrypoint.sh`) zusätzlich +3 / +6 / +6 grep
counts für `context_pct` / `CONTEXT_MAX` (Plan 06-03).

**Phase-1-Contract:** Race tests `test_dispatch_race.py` blieben über alle 8
Phase-6-Plans hinweg 3/3 grün — REF-03 Behaviour-Preservation Contract erfüllt.

**Live-Smoke:** Der manuelle Chaos-Test des Operators (Plan 06-07 Task 3 Schritt 6) ist
deferred — strukturell ist alles verifiziert (siehe
`.planning/notes/phase-6-success-criteria.md`). Outcome wird hier nachgetragen
nachdem der Operator "approved" gibt.

**Operator Merge Hash:** Wird hier nachgetragen nachdem `gsd/phase-6-context-management-auto-recovery`
nach `main` gemerged ist (per Plan 06-07 Task 3 Sign-off).

Soak-Beobachtung: 7-Tage-Window nach Merge wird beobachtet ob
`agent.compaction` Events als Auto-Maintenance auftauchen (Erwartung: 1-3 pro
Agent pro Woche bei normalem Betrieb).

## Rollback

Kill-Switch: `settings.context_compaction_enabled: bool = True` (Default-on).
Auf False setzen + `docker compose restart backend` → CTX-02 fallback auf
Bug-kompatibles `_reset_overflowed_sessions` (alte Methode bleibt im Code als
Reference, mit `# DEPRECATED Plan 06-04` Marker). REC kein Kill-Switch noetig
— Stale-Pfad bleibt rueckwaertskompatibel zur Approval-Eskalation als Tier 4
Fallback.

## Referenzen

- `.planning/phases/06-context-management-auto-recovery/06-CONTEXT.md`
- `.planning/phases/06-context-management-auto-recovery/06-PATTERNS.md`
- `.planning/phases/06-context-management-auto-recovery/06-UI-SPEC.md`
- ADR-013 (Docker-V2 tmux-Layout) — Window-Topologie unangetastet
- ADR-024 (Claude-Process Recycling) — Recycler komplementaer zu Compaction
- CLAUDE.md "Absolute Verbote" — `get_session_context_for_runtime` Pflicht
