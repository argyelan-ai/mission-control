# Lifecycle Safety Watchdog — Silent-Abort Auto-Block (ADR-046)

**Branch:** `feat/lifecycle-safety-watchdog` (isolierter Worktree `.worktrees/lifecycle-watchdog/`)
**Status:** fertig gebaut, alle Tests grün, **nicht gemergt** (Merge = des Operators Gate)
**Datum:** 2026-07-01

---

## 1. Exec Summary — ehrliche Bewertung

**Schliesst der Watchdog die Silent-Abort-Lücke robust für ALLE Runtimes? → Nein, bewusst nur für `cli-bridge` (v1). Und das ist richtig so.**

- **Das Problem:** Ein Agent PATCHt `status=in_progress` (ackt), verstummt dann aber ohne je einen terminalen PATCH (`review`/`blocked`/`failed`) zu senden → Task hängt für immer in `in_progress`. Der bestehende `_check_stale_in_progress`-Circuit-Breaker feuert nur ein `task.stuck`-Event und lässt den Task hängen — die **terminale Sprosse fehlte**.
- **Was jetzt zuverlässig geschlossen ist:** die `cli-bridge` (Docker) Runtime — die einzige, die `last_task_activity_at` **während** der Arbeit per poll.sh-Working-Heartbeat (Bug-13-Fix) stempelt. Nur dort ist das Liveness-Signal vertrauenswürdig.
- **Bewusst ausgeklammert (Guard 0):** `host` (launchd/Boss/Hermes/Jarvis) und `manual`/`claude-code`. Deren `poll.sh` sendet nur einen einmaligen `working`-Heartbeat beim Dispatch und danach unbedingt `idle` → `last_task_activity_at` **friert beim ACK ein**. Würde der Watchdog dort greifen, würde er **jeden gesunden Host-Turn > 25 min fälschlich blocken** (Prime-Directive-Verletzung). Ein früher Design-Claim „host covered by construction" war **faktisch falsch** und wurde nach Code-Trace verworfen.
- **Fazit:** Ehrlich betrachtet ist das eine **teilweise, aber sichere** Lösung. Die Lücke ist für die verbreitetste Worker-Runtime (cli-bridge) zu, für Host bleibt sie offen bis der Working-Heartbeat nach `boss-host/poll.sh` portiert ist (ADR-046 Open Question). Der Tradeoff folgt der Prime Directive: **lieber eine Runtime sauber schliessen als alle unsicher.**

---

## 2. Was gebaut wurde

| Datei | Änderung |
|---|---|
| `backend/app/services/task_runner.py` | **Kern.** Konstanten `STUCK_BLOCK_MINUTES=25` / `_SLOW=45` / `MIN_STUCK_BLOCK_FLOOR`; Helper `_stuck_block_default_for`, `_stuck_block_threshold_for` (gefloorter Per-Agent-Override), `_liveness_floor_seconds`; neue Leaf-Methode `_check_stuck_in_progress(session)` (task_runner.py:1066); Wiring in `_check_tasks()` bei :315 **nach** `_check_stale_in_progress`. |
| `backend/app/redis_client.py` | `task_runner_stuck_block(task_id)` (24h-Dedup) + `task_runner_stuck_block_count(task_id)` (≥2-Tick-Persistenz-Zähler). Grep-geprüft kollisionsfrei. |
| `backend/app/config.py` | `lifecycle_watchdog_enabled: bool = True` (:162) — Fleet-weiter Kill-Switch. **Der Threshold selbst** bleibt per-Agent in `dispatch_config` (ADR-031-Muster), nur on/off in settings. |
| `backend/tests/conftest.py` | `lifecycle_watchdog_enabled` im Pre-Import-Monkeypatch gepinnt (99999-Interval-Konvention). |
| `backend/tests/test_stuck_in_progress_watchdog.py` | **24 Tests** (5 Threshold-Helper + 19 Behavioral, SQLite in-memory + fakeredis). |
| `docs/decisions/046-lifecycle-safety-watchdog.md` | ADR-046 (Accepted), Alternativen A–D. |
| `docs/decisions/README.md` | ADR-046-Zeile + fehlende ADR-041-Zeile ergänzt. |
| `docs/ARCHITECTURE.md` | Services-Table, Übersicht, Task-Lifecycle Step 4, ADR-Übersicht, „Wo ändere ich was?", Änderungshistorie. |
| `docs/code-map.md` | Regeneriert via `tools/generate-code-map.py`. |

**Der Check (Hook-Point):** Peer-Methode `_check_stuck_in_progress` läuft im bestehenden 60s-`task_runner`-Tick, unter dem bestehenden `task_runner_lock` (NX Redis, Single-Worker) — **kein neuer Loop, kein neues Interval-Setting**. Platziert **nach** der Tiered-Recovery, damit Restart+Resume immer zuerst versucht wurde.

---

## 3. Wie der Gap schliesst — Prädikat + Aktion

**Prädikat (alle müssen halten, auf ≥2 aufeinanderfolgenden Ticks):**

| # | Guard | Zweck |
|---|---|---|
| **0** | `agent.agent_runtime == "cli-bridge"` | **Die FP-Firewall.** Host/manual hart übersprungen. |
| 1–11 | `in_progress` + assigned + `ack_at` gesetzt + `blocked_by_task_id` NULL + run_control ≠ stopped/manual_hold + review ≠ hold + **keine Kind-Subtasks** (Leaf) + nicht `is_board_lead` + `TASKS_WRITE`-Scope + `operational_mode` ≠ paused + run_state ∉ running/recovering | By-Design-Waiter & Orchestratoren raus |
| 12 | **WRAPPER ALIVE:** `now − last_seen_at < 2× heartbeat` (min 120s) | Wenn `last_seen` **auch** stale → Prozess tot → `_recover_orphaned_tasks` (→Inbox), **nicht** blocken |
| 13 | **DEAD TURN:** `now − last_task_activity_at ≥ stuck_block_minutes` | Das **einzige** verlässliche „Wrapper lebt, LLM-Turn tot"-Delta |
| 14 | **KORROBORATION:** kein agent-authored TaskComment im Fenster | frischer Progress-Kommentar hebt den Block auf |
| 15–17 | keine pending `blocker_decision`/`agent_stuck`-Approval (DB-Fallback, restart-safe) + letzter Kommentar kein poll.sh-Blocker + Redis-Dedup-Key ungesetzt | Idempotenz |

**Guards 12+13 zusammen sind der Silent-Abort-Fingerabdruck:** `last_seen` **frisch** UND `last_task_activity` **stale**.

**Threshold-Auflösung** `_stuck_block_threshold_for(agent)`: (1) `dispatch_config['stuck_block_minutes']`-Override — **gefloort** auf `max(role_idle, 20)` → (2) runtime-aware Default 25 min (claude) / 45 min (slow/local) → (3) hard fallback. Invariante: **immer ≥ role idle + Marge**, also lief die Tiered-Recovery garantiert zuerst und scheiterte.

**Aktion (gestaffelt):**
- **Tick 1:** system-`TaskComment`-Nudge („geackt aber seit N min kein Fortschritt — bitte Status setzen"). **Kein Block.** Persistenz-Zähler +1.
- **Tick 2+:** `apply_terminal_unassign(session, task, 'blocked')` (kanonischer Pfad — **behält `assigned_agent_id`** → resumable, gibt `current_task_id` frei, setzt `run_state='blocked'`) + `record_task_event(changed_by='watchdog', reason='stuck_no_terminal_patch')` + `Approval(action_type='blocker_decision', payload={blocker_type:'technical_problem', question, ...}, expires_at=+24h)` + `telegram_bot.send_approval_telegram(...)` → Operator + `emit_event('task.status_changed', severity='warning')` + Redis-Dedup-Key 24h. **NIE `failed`.** Der geblockte Task speist die bestehende `_check_blocked_tasks`-Bahn → Operator.

**Beleg (passender Test):** `test_blocks_silent_abort` beweist den Block-Pfad (assigned_agent_id **behalten**, current_task_id freigegeben, run_state='blocked'). Voller Lauf: **`24 passed in 1.73s`** (frisch verifiziert im Worktree-venv).

---

## 4. FALSE-POSITIVE-STORY — warum ein gesunder Langläufer NIE geblockt wird

des Operators Hauptsorge. Der Block ruht **nie auf einem einzelnen stalen Feld**, sondern auf der **einen Liveness-Delta** — mehrschichtig abgesichert, jede Schicht einzeln getestet:

1. **RUNTIME-GATE (Guard 0) = Firewall.** Das schwache `last_task_activity_at`-Proxy wird **nur auf cli-bridge** überhaupt vertraut — der einzigen Runtime, die es während der Arbeit auffrischt. → `test_never_blocks_host_agent`, `test_never_blocks_manual_runtime` (beide setzen 40 min stale Activity, die Guard 13 *auslösen würde* — Guard 0 vetot).
2. **Die DELTA, kein Einzelfeld.** Bei einem echten langen (5-min) Tool-Call laufen **BEIDE** Timestamps weiter (~alle 30s): `last_seen_at` (poll.sh-Heartbeat unbedingt) UND `last_task_activity_at` (Claude-TUI zeigt Working-Marker `esc to interrupt` / `● Bash(` / Spinner → `detect_turn_state=='working'` → gestempelt). Meanwhile `task.updated_at`, `ack_at`, Kommentar-Alter **frieren ein** — deshalb keyt das Design **absichtlich NICHT** darauf (die dokumentierte FP-Falle des Legacy-Checks). → `test_never_blocks_healthy_long_turn` (3 Ticks, nie geblockt).
3. **Konservativer, geflorter, runtime-aware Threshold.** Default 25 min (claude) / **45 min (slow/local)** — deutlich über dem 180s-poll.sh-Stagnation-Fenster und dem 15-min-cli-bridge-ACK-Fenster. Deckt die Sparky-„12-min-cook"-Klasse. → `test_never_blocks_slow_runtime_under_45min`. Per-Agent-Override **code-geklemmt** auf ≥20 → `test_stuck_block_threshold_override_is_floored` (Override=5 löst zu ≥20 auf).
4. **Dead-Process-Disambiguierung.** Ist `last_seen_at` **auch** stale → Prozesstod → Orphan→Inbox-Pfad, **nicht** Block. → `test_never_blocks_dead_process`.
5. **Gestaffelte Eskalation.** Tick 1 nur Nudge; Block braucht Persistenz über ≥2 Ticks (Redis-Zähler) → absorbiert Netz-Blips, gibt dem Agent + poll.sh eine Chance zum Selbst-Melden.
6. **Korroboration.** Frischer Agent-Progress-Kommentar unterdrückt den Block. → `test_skips_recent_agent_progress_comment`.
7. **Alle By-Design-Waiter übersprungen:** board-lead, parent-with-children, callback-wait, review-hold, run_control-stopped, paused, run_state=running. → je eigener `test_skips_*`.

Bei marginaler Confidence fällt das Design auf ein **gesurfactes `task.stuck`-Warning statt Block** zurück. Kill-Switch `lifecycle_watchdog_enabled` deaktiviert den ganzen Check → `test_kill_switch_disables_check`. **Eine sichtbare Warnung schlägt immer einen fälschlichen Block.**

---

## 5. Risiken & ehrliche Limitierungen

| Risiko | Ehrliche Einschätzung |
|---|---|
| **Liveness-Signal ist prinzipiell schwach** | `last_task_activity_at` ist ein Proxy für tmux-Pane-Scraping (`detect_turn_state`) mit **dokumentierten False-Negatives während langer LLM-*Reasoning*-Phasen** (statischer Pane, keine Tool-Marker). Ein echter Reasoning-Cook > Threshold mit lebendem Wrapper *könnte* geblockt werden. Mitigation: hoher runtime-aware Threshold + Staffelung + blocked-statt-failed + Operator-Notify + Kill-Switch. Real selten (Claude reasoned selten 25 min mit null Output). |
| **Host/manual NICHT abgedeckt** | Bewusst (Guard 0). Silent-Abort auf Host bleibt offen bis Bug-13-Working-Heartbeat nach `boss-host/poll.sh` portiert ist. Guard 0 vor dann zu weiten = **explizit verboten**. |
| **Fail-open bei unauflösbarer Runtime** | Wenn `runtime_id` NULL/unauflösbar, greift der schnelle 25-min-Default statt slow-45. Ein Verify-Judge empfahl fail-safe `slow=True` bei unbekannter Runtime — **offener Verbesserungspunkt** (noch nicht umgesetzt). |
| **Threshold-Default (25/45) ist empirischer Guess** | Nicht garantiert, gegen echte Silent-Abort-Incidents zu validieren (ADR Open Question). Empfehlung: Telemetry auf jeden Block loggen. |
| **Persistenz-Zähler wird bei Recovery nicht geresettet** | Stall→healthy→Stall innerhalb 1h TTL überspringt den 2. Nudge-Tick. Schwächt nur die Grace, nicht die Prime-Directive-Sicherheit. Kosmetisch. |
| **Doppelter in_progress-Scan/Tick** | stale-check + stuck-check enumerieren beide. Bei aktuellem Volumen vernachlässigbar; Partial-Index `ix_tasks_stuck` als Option vorbereitet, deferred. |

**Regression:** Full Suite `2478 passed, 12 failed, 8 errors` — die 12+8 sind **alle pre-existing & unrelated** (test_hermes_skill FileNotFoundError, test_model_prices unseeded, mc_cli/mcp fixtures); keine importiert task_runner/redis_client/lifecycle. Da alle 2478 anderen grün sind, laden die config.py/conftest.py-Additions sauber.

---

## 6. Merge-Checklist für den Operator

- [ ] **Branch:** `feat/lifecycle-safety-watchdog` im Worktree `.worktrees/lifecycle-watchdog/` (Main-WIP unangetastet).
- [ ] **Tests laufen lassen** (Worktree-venv):
      `cd .worktrees/lifecycle-watchdog/backend && .venv/bin/python -m pytest tests/test_stuck_in_progress_watchdog.py -q` → erwartet **24 passed**.
- [ ] **ADR-046** vorhanden (`docs/decisions/046-lifecycle-safety-watchdog.md`, Accepted) + im README-Index.
- [ ] **ARCHITECTURE.md** aktualisiert (Services-Table, Task-Lifecycle Step 4, ADR-Übersicht, Änderungshistorie).
- [ ] **Review-Fokus (das Wichtigste):**
      1. `_check_stuck_in_progress` in `task_runner.py:1066` — v.a. **Guard 0** (cli-bridge-Gate, :~1120) und **Guards 12/13** (Liveness-Delta).
      2. `_stuck_block_threshold_for` (:208) — der **Override-Floor** (Prime-Directive-Klemme).
      3. Die 3 PRIME-DIRECTIVE-Tests: `test_never_blocks_healthy_long_turn`, `test_never_blocks_host_agent`, `test_never_blocks_dead_process`.
- [ ] **Merge = dein Entscheid.** Empfehlung: erst mit `lifecycle_watchdog_enabled=False` deployen (Kill-Switch), Telemetry beobachten, dann scharf schalten.
- [ ] **Kein Deploy/Docker/DB durch mich** — reiner Code+Docs-Stand im Worktree.

---

## 7. Offene Fragen

1. **Host-Coverage:** Bug-13-Working-Heartbeat in `boss-host/poll.sh` (+ Hermes/Jarvis) portieren, DANN Guard 0 weiten? (Aktuell bewusst offen.)
2. **Default 25/45 min** gegen echte Silent-Abort-Incidents validieren — Host evtl. längeres Fenster als cli-bridge.
3. **Fail-safe bei unauflösbarer Runtime:** unbekannt → slow-45 (konservativ) statt fast-25?
4. **Eine Re-Dispatch-Runde vor Eskalation** an den Operator, oder ist die bereits gelaufene Tiered-Recovery genug? (Aktuell: Recovery lief → Block geht direkt zum Operator.)
5. **Partial-Index `ix_tasks_stuck`** jetzt shippen oder deferren bis Query-Logs es zeigen?
6. **Persistenz-Zähler-Reset** bei beobachteter Recovery nachrüsten (kosmetisch).
