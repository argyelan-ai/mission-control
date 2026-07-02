# ADR-046 — Lifecycle Safety Watchdog (Silent-Abort Auto-Block)

**Status:** Accepted
**Datum:** 2026-07-01
**Scope:** Backend/Task-Runner · Backend/Watchdog

## Kontext

Ein Agent kann eine Task acken (`PATCH status: in_progress` → `ack_at` gesetzt) und danach **still verstummen**, ohne je einen terminalen `PATCH` auf `review` / `blocked` / `failed` zu senden. Ursachen: die LLM-Runde bricht ab (API-Fehler, Crash im TUI, Kontext-Reset), der Agent "vergisst" den Abschluss, oder er läuft in einen Zustand, aus dem er nicht selbst herausfindet. Die Task bleibt dann **für immer `in_progress`** — sie taucht in keiner Eskalations-Lane auf, blockiert die Phase, und der Operator bemerkt es erst, wenn er zufällig hinschaut.

Das ist der **Silent-Abort-Bug**. Er betrifft grundsätzlich **alle Runtimes** (cli-bridge Docker + host launchd). **v1 schliesst ihn jedoch nur für cli-bridge** — host ist ein bewusst zurückgestellter Follow-up (siehe Runtime-Abdeckung), weil der Host-Runner heute kein Liveness-Signal liefert, das *während* einer Runde aktualisiert wird. Ihn trotzdem einzubeziehen wäre eine Prime-Directive-Verletzung.

Heute gibt es zwei benachbarte, aber unvollständige Mechanismen:

- `task_runner._check_stale_in_progress` (task_runner.py:811) erkennt idle `in_progress`-Tasks (kein Kommentar seit Rollen-Idle-Threshold) und startet **tiered recovery** (Restart + Recovery-Recap). Nach `MAX_STALE_CHECKS` emittiert der Circuit-Breaker (task_runner.py:927-959) ein `task.stuck`-Event (severity=error) — **lässt die Task aber unendlich `in_progress`**. Genau diese fehlende letzte Sprosse der Leiter schliessen wir.
- `watchdog._recover_orphaned_tasks` (task_monitor.py:1236) fängt den **anderen** Fall: der ganze Agent-Prozess ist tot (`last_seen_at` > 30min) → Task zurück nach `inbox`. Das ist **Prozess-Tod**, nicht Silent-Abort bei lebendem Wrapper.

Die harte Randbedingung (**Prime Directive**): Ein Agent, der **genuin noch arbeitet** — ein langer Tool-Call (Build, `git clone`, grosser Testlauf, Browser-Automation) oder eine lange LLM-Runde — darf **niemals** fälschlich geblockt werden. Ein False Positive, der einen gesunden Agenten blockt, ist **schlimmer** als der Bug selbst.

Der Kern der Schwierigkeit: MC hat **kein starkes Liveness-Signal** für "die LLM-Runde denkt gerade nach". Das beste verfügbare Signal ist `agent.last_task_activity_at` (models/agent.py:149) — es wird nur auf dem **Working-Path** des Heartbeats gestempelt (agents.py:2427-2430), d.h. wenn poll.sh's `detect_turn-state()` (turn-state.sh) im tmux-Pane Arbeits-Marker sieht (`esc to interrupt`, `● Bash(`, `✻`-Spinner). Dieses Signal ist ein **Proxy auf tmux-Pane-Scraping-Heuristiken** mit **dokumentierten False-Negatives** bei langem Reasoning (der "Sparky 12-Minuten-Cook" False-Positive zwang poll.sh, `STAGNATION_THRESHOLD` von 60s auf 180s zu erhöhen, poll.sh:52-58). Ein Backend-Threshold darauf muss daher **deutlich konservativer** sein als das poll.sh-Fenster und **korroboriert** werden.

## Entscheidung

Wir fügen einen **Lifecycle-Safety-Watchdog (v1 cli-bridge-gated, Guard 0)** als neue Methode `_check_stuck_in_progress(session)` in `task_runner.py` hinzu, aufgerufen aus `_check_tasks` (task_runner.py:225) **unmittelbar nach** `_check_stale_in_progress`. Er läuft im bestehenden 60s-Task-Runner-Tick — **kein neuer asyncio-Loop, kein neues `settings.*`-Intervall**.

Er blockt eine Task **nur dann** automatisch, wenn **alle** folgenden Bedingungen erfüllt sind (Predicate voll in §"Predicate" des Design-Docs), und **erst nach einem Nudge-Tick** (staged escalation, nie sofort):

0. **RUNTIME-GATE (FP-Firewall, zuerst geprüft):** `agent.agent_runtime == "cli-bridge"`. Nur diese Runtime stempelt `last_task_activity_at` *während* der Arbeit (shared/poll.sh Bug-13). host / manual / claude-code werden in v1 **hart übersprungen** — sie einzubeziehen hiesse einen gesunden Langläufer zu blocken.
1. `status == in_progress` UND `assigned_agent_id IS NOT NULL` UND `ack_at IS NOT NULL` (Agent hat wirklich gestartet).
2. Kein `blocked_by_task_id` (kein Callback-Wait), `run_control NOT IN (stopped, manual_hold)`, `review_decision != hold`.
3. Keine Subtasks (Parents warten by design), Agent **nicht** `is_board_lead`, `Scope.TASKS_WRITE ∈ effective scopes` (Orchestratoren/Planner raus).
4. **WRAPPER LEBT:** `agent.last_seen_at` frisch (`now - last_seen_at < 2 × heartbeat-interval`, Floor ≈ 2 min). Ist `last_seen_at` **auch** stale → Prozess tot → gehört `_recover_orphaned_tasks` (→ inbox), **nicht** hier.
5. **TURN TOT:** `agent.last_task_activity_at` stale über einen **konservativen** Threshold (`stuck_block_minutes`, Default deutlich über dem Rollen-Idle-Threshold, z.B. 25 min).
6. **KORROBORATION:** kein agent-authored `TaskComment` innerhalb desselben Fensters.
7. **IDEMPOTENZ:** keine offene `Approval(action_type in blocker_decision|agent_stuck)`, kein poll.sh-Auto-Blocker als letzter Kommentar, Redis-Dedup-Key (24h).

Aktion bei Trigger: **`apply_terminal_unassign(session, task, "blocked")`** aufrufen (der kanonische Pfad — task_lifecycle.py:206-226, von PR #107/#111 als *Pflicht für ALLE blocked-setzenden Pfade* geschrieben, um die stille Cancel-Schleife zu verhindern). Für unseren Fall (Guard 4 garantiert `blocked_by_task_id IS NULL` = Human-Wait) **behält** der Helper `assigned_agent_id` (Task reversibel/wiederaufnehmbar) und **gibt gleichzeitig `agent.current_task_id = None` frei + setzt `agent.run_state = "blocked"`** — so erscheint der Agent nicht länger als busy gegenüber Boss/Dispatch und der nächste `/agent/me/poll` kann die Cancel-Schleife nicht auslösen. Danach `task.status = "blocked"` setzen (der Helper setzt den Status nicht selbst), `record_task_event(from="in_progress", to="blocked", changed_by="watchdog", reason="stuck_no_terminal_patch")`, eine `Approval(action_type="blocker_decision")` mit `blocker_type="technical_problem"` + konkreter `blocker_question` an den Operator (Telegram + `approval.created` Event), plus `task.stuck`-Event. Das speist die bestehende `_check_blocked_tasks`-Lane (task_monitor.py:684) und den Inbox-Badge.

**Korrektur zur früheren Fassung:** Ein früherer Entwurf setzte `status="blocked"` von Hand und mied `apply_terminal_unassign` bewusst — das war ein Fehl-Lesen des Helpers. Für `blocked` **unassignt** der Helper nicht (behält `assigned_agent_id`), gibt aber im Human-Wait-Fall den Agent-Lock frei und setzt `run_state`. Von Hand `status="blocked"` zu setzen liess `current_task_id` an der geblockten Task hängen (Agent für immer scheinbar busy) und riskierte genau die Cancel-Schleife, die der Helper verhindert. Für `blocked` ist `apply_terminal_unassign` daher der vorgeschriebene Pfad, nicht der vermiedene.

**Runtime-Abdeckung (v1 = nur cli-bridge; host bewusst zurückgestellt):** Ein früherer Entwurf behauptete "host und cli-bridge sind by construction beide abgedeckt, kein runtime-spezifischer Branch nötig". **Diese Behauptung ist faktisch falsch und würde die Prime Directive verletzen** — die beiden DB-Timestamps sind nur dann äquivalent, wenn beide Runner sie *während* der Arbeit gleich *stempeln*, und das tun sie nicht:

- **cli-bridge (docker/shared/poll.sh) — IM SCOPE.** shared/poll.sh hat den Bug-13-Fix (2026-05-13, poll.sh:685-701): bei aktivem Task ruft es `detect_turn_state` und sendet `heartbeat "working"`, wenn der Pane Arbeits-Marker zeigt. Der Endpoint stempelt dann `last_task_activity_at` auf dem Working-Path (agents.py:2427-2434). Gesunde Runde → frisch; Silent-Abort → friert ein, während `last_seen_at` frisch bleibt → Predicate feuert korrekt. Schliesst **Sparky** ein (openclaude nutzt ebenfalls shared/poll.sh) → daher der runtime-bewusste 45-min-Default für langsame lokale Modelle.
- **host (Boss/Hermes/Jarvis via launchd) — v1 AUSSER SCOPE.** docker/boss-host/poll.sh sourcet turn-state.sh **nicht** (poll.sh:366) und sendet im Steady-State-Loop **bedingungslos** alle 30s `heartbeat "idle"` (poll.sh:348-353). `heartbeat "working"` sendet es genau **einmal**, beim Dispatch (poll.sh:150). Bei einem idle-Payload mit aktivem in_progress-Task nimmt der Endpoint den else-Zweig: er erzwingt `run_state='idle'` und stempelt `last_task_activity_at` **nicht**. NET: für jeden host-Agent **friert `last_task_activity_at` zum ACK-Zeitpunkt ein** für die ganze Runde, während `last_seen_at` frisch bleibt. Guard 13 würde also bei **jeder** host-Task feuern, die länger als `stuck_block_minutes` läuft, obwohl der Agent kerngesund ist — eine lehrbuchmässige Prime-Directive-Verletzung. (Die heutige Fleet ist nur *zufällig* geschützt, weil Boss `is_board_lead` ist → Guard 8; darauf dürfen wir uns nicht verlassen.)
- **manual / claude-code — AUSSER SCOPE.** Kein Working-Heartbeat → gleiches Freeze-Problem.

**Entscheidung: Guard 0 gated den gesamten Check auf `agent.agent_runtime == "cli-bridge"`.** Es gibt heute kein host-Liveness-Signal, das *während* einer host-Runde aktualisiert wird — es gibt also nichts Sicheres, worauf man keyen könnte.

**Voraussetzung für spätere host-Abdeckung (Follow-up, nicht dieses ADR):** den Bug-13-Working-Heartbeat in docker/boss-host/poll.sh (+ Hermes/Jarvis) portieren (turn-state.sh sourcen, `heartbeat "working"` bei `detect_turn_state=='working'`, analog shared/poll.sh:685-701). Erst *dann* darf Guard 0 auf `"host"` erweitert werden.

## Alternativen

- **Alternative A — In `_check_stale_in_progress` einfalten (kein separater Check):** Die Block-Sprosse direkt in den Circuit-Breaker-Zweig (task_runner.py:932) hängen. → **Verworfen**, weil die Block-Bedingung ein **eigenes, strengeres Multi-Signal-Gate** (Wrapper-lebt + Turn-tot-Delta), einen **eigenen Config-Knopf** (`stuck_block_minutes`) und einen **eigenen Dedup-Key** braucht. Einfalten würde die Idle-Recovery-Semantik mit der Terminal-Block-Semantik vermischen und künftige Änderungen an einem Pfad würden den anderen still brechen (dieselbe Begründung wie `_get_ack_timeout_minutes` bewusst separat von `_get_agent_timeout`, task_runner.py:153-156).

- **Alternative B — Nur `task.stuck`-Event (severity=error) emittieren, nie auto-blocken:** Der heutige Circuit-Breaker-Zustand. → **Verworfen**, weil er den Bug **nicht behebt**: Die Task bleibt `in_progress`, blockiert die Phase, und das Event verschwindet im Rauschen, wenn der Operator nicht hinschaut. Der Block ist die fehlende **terminale Sprosse** — er schiebt die Task in eine Lane, die den Operator aktiv per Telegram erreicht. (Bei **marginaler** Confidence fällt der Check aber bewusst auf genau dieses `task.stuck`-Event zurück statt zu blocken — B bleibt der sichere Fallback, nicht die Lösung.)

- **Alternative C — Auto-Reset nach `inbox` (`_recover_orphaned_tasks`-Muster):** Wie beim Prozess-Tod die Task zurück in die Queue werfen. → **Verworfen**, weil der Agent **lebt** (`last_seen_at` frisch) und die Task evtl. teilweise erledigt ist. `inbox`-Reset würde stillen Fortschritt wegwerfen und die Task blind re-dispatchen (mit dem Risiko der "gekürzter-Prompt-Reaktivierung", die CLAUDE.md ausdrücklich verbietet). `blocked` **behält** `assigned_agent_id`, ist reversibel und notifiziert den Operator — die least-destructive Aktion.

- **Alternative D — Neuer dedizierter Liveness-Stream (omp-bridge / Redis-Heartbeat-Key pro Agent):** Ein zusätzlicher, hochfrequenter Kanal (z.B. ein Redis-TTL-Key, den poll.sh bei jeder LLM-Token-Emission refresht, oder ein gestreamter Bridge-Event), der ein **starkes** "LLM lebt gerade"-Signal liefert und das schwache `last_task_activity_at` ersetzt. → **Verworfen für v1 als zu schwer:** Es gibt heute **keinen** solchen Redis-Liveness-Key (redis_client.py:98-137 sind nur Locks + Dedup); poll.sh hittet `/agent/me/heartbeat`, das nur DB schreibt. Ein neuer Stream bräuchte Änderungen an poll.sh, turn-state.sh, dem Heartbeat-Endpoint **und** einer neuen Redis-Infrastruktur mit eigener Ausfall-Semantik — und würde am Grundproblem (tmux-Pane-Scraping hat False-Negatives bei langem Reasoning) **nichts** ändern, nur die Frequenz erhöhen. Der konservative Threshold + Korroboration + Staged-Nudge auf den **bestehenden** DB-Signalen liefert dieselbe False-Positive-Sicherheit ohne neue bewegliche Teile. Der Stream bleibt als **künftige Härtung** notiert (Open Question), falls das DB-Signal sich als zu grob erweist.

## Konsequenzen

### Positiv
- Der Silent-Abort-Bug ist **für cli-bridge (inkl. Sparky) geschlossen** — eine acked-dann-verstummte cli-bridge-Task landet garantiert in einer Operator-sichtbaren Lane statt ewig `in_progress` zu hängen. Deckt auch **ad-hoc/One-Shot-Tasks ohne Parent** ab (Prefilter fordert keinen Parent, Leaf-Erkennung Python-seitig wie `_check_stale_in_progress`).
- **Kein neuer Loop, kein neues Intervall:** erbt den 60s-Tick, Single-Worker-Redis-Lock und die AsyncSession des Task-Runners — minimale operationale Fläche.
- **FP-sicher gated:** Guard 0 beschränkt auf `cli-bridge`, die einzige Runtime, deren `last_task_activity_at` *während* der Arbeit gestempelt wird. Der schwache Proxy wird nie einer Runtime unterstellt, die ihn nicht liefert.
- **Reversibel + Lock-sauber:** `apply_terminal_unassign(…, "blocked")` behält `assigned_agent_id`, gibt aber den Agent-Lock frei (`current_task_id=None`, `run_state='blocked'`) — kein verlorener Fortschritt, kein scheinbar-busy-Agent, keine Cancel-Schleife.
- **False-Positive-sicher gestaffelt:** Nudge zuerst, Block erst wenn die Bedingung über ≥2 Ticks persistiert; bei marginaler Confidence nur `task.stuck` statt Block. Threshold-Override ist **code-seitig gefloort** (nie < max(role_idle, 20)).

- **host bleibt v1 ungedeckt** (bewusst — siehe Runtime-Abdeckung). Ein silent-abortender host-Agent hängt weiterhin `in_progress`, bis der Bug-13-Port in boss-host/poll.sh landet und Guard 0 erweitert wird. Das ist die akzeptierte Under-Coverage im Tausch gegen die Prime-Directive-Sicherheit (lieber ein ungedeckter host-Fall als ein fälschlich geblockter gesunder host-Agent).

### Negativ
- **Der Liveness-Proxy bleibt schwach.** `last_task_activity_at` ist tmux-Pane-Scraping mit dokumentierten False-Negatives bei langem Reasoning. Wir kompensieren mit (a) Guard 0 (nur cli-bridge, wo das Signal überhaupt gestempelt wird), (b) einem **runtime-bewussten** Default (25 min claude / 45 min langsame lokale Modelle wie Sparky) und (c) Korroboration + Staging. Der dispatch_config-Override ist **code-seitig gefloort** (`max(override, max(role_idle, 20))`) — ein zu niedrig gesetzter Override kann einen gesunden Agenten **nicht mehr** blocken (früher als akzeptiertes Risiko dokumentiert, jetzt durchgesetzt + Test `test_stuck_block_threshold_override_is_floored`). Restrisiko: jede Änderung am poll.sh-Turn-State-Detektor kann das Kalibrat verschieben — bei solchen Änderungen den Default neu prüfen.
- **Latenz:** ein wirklich stuck Agent wird frühestens nach `stuck_block_minutes` + einem Nudge-Tick geblockt (Grössenordnung ~30 min, bei langsamen Runtimes ~50 min). Das ist der bewusste Preis der False-Positive-Sicherheit.
- **Neuer Redis-Namespace** (`mc:task_runner:stuck_block:*`) muss kollisionsfrei bleiben (grep-Beweis, ADR-026-Präzedenz).
- **Doppel-Trigger-Risiko mit poll.sh:** poll.sh kann denselben Fall client-seitig zu `blocked` flippen. Idempotenz-Guard (Punkt 7) muss das abfangen, sonst zwei Blocker-Approvals für dieselbe Task.
- **Optionaler Index:** effizient wird der DB-Prefilter erst mit einem partiellen Index `ix_tasks_stuck ON tasks(updated_at) WHERE status='in_progress'` — ein Schema-Change (Migration). Ohne ihn ist der Scan bei kleiner Task-Zahl aber unkritisch.

## Referenzen

- Betroffene Dateien:
  - `backend/app/services/task_runner.py:225` (Hook in `_check_tasks`), `:811` (`_check_stale_in_progress` Sibling), `:927-959` (Circuit-Breaker = fehlende Sprosse), `:67` (`_idle_threshold_for` Muster), `:146` (`_get_ack_timeout_minutes` Muster), `:579` (`_create_dispatch_approval` Eskalations-Muster)
  - `backend/app/services/task_lifecycle.py:40` (`record_task_event`), `:163-226` (`apply_terminal_unassign` — Pflichtpfad für blocked, Human-Wait gibt Lock frei + `run_state='blocked'`, behält `assigned_agent_id`)
  - `backend/app/services/task_runner.py:848-853` (`_check_stale_in_progress` Leaf-Guard = "keine Child-Subtasks" in Python, NICHT "hat Parent" — Vorbild für Prefilter/Guard 7)
  - `backend/app/routers/agents.py:2427-2434` (Heartbeat: `last_task_activity_at` nur auf Working-Path; idle-else-Zweig erzwingt `run_state='idle'`, stempelt nicht → Grund warum host v1 ausser Scope)
  - `docker/shared/poll.sh:685-701` (Bug-13 Working-Heartbeat — cli-bridge IM Scope) vs. `docker/boss-host/poll.sh:150,348-353,366` (kein turn-state.sh, bedingungslos idle, `working` nur beim Dispatch — host AUSSER Scope)
  - `backend/app/redis_client.py:127-137` (RedisKeys `task_runner_stale*` → neuer `task_runner_stuck_block`)
  - `backend/app/models/task.py:24,60,161,200` (`status`, `ack_at`, `blocked_by_task_id`, `TaskEvent`)
  - `backend/app/models/agent.py:88,146,149` (`heartbeat_config`, `last_seen_at`, `last_task_activity_at`)
  - `backend/app/routers/agent_task_status.py:2124-2182` (Blocked-Side-Effects: Approval + Telegram + lead-notify — Muster für die Watchdog-Aktion)
  - `backend/app/services/watchdog/task_monitor.py:684` (`_check_blocked_tasks`, Ziel-Lane), `:1236` (`_recover_orphaned_tasks`, Abgrenzung Prozess-Tod)
  - `docs/ARCHITECTURE.md` (Services-Tabelle, Task-Lifecycle Step 4, ADR-Übersicht, Änderungshistorie)
- Verwandte ADRs: ADR-008 (Phase-Completion-Watchdog, Redis-Lock/Idempotenz-Muster), ADR-026 (Context Management & Auto-Recovery, Tiered-Recovery-System das dieser Check erweitert), ADR-031 (Per-Agent idle_timeout — direkter Präzedenzfall "gesunden Langläufer nicht killen"), ADR-035 (`dispatch_attempt_id`)
- Design-Doc: `docs/plans/lifecycle-safety-watchdog-design.md`
- Externe Quellen: poll.sh:52-58 (Sparky-12-min-Cook False-Positive → STAGNATION_THRESHOLD 60s→180s)

## Amendment 2026-07-02 — run_state-Skip entfernt (Incident omp-Zombie)

Beim OMP-Go-Live blieb ein echter Silent-Abort (Bridge von defektem Recycler
gekillt, Task acked+stumm 70min) ungeblockt: der "opportunistic skip" bei
``run_state in ('running','recovering')`` uebersprang den Check dauerhaft,
weil run_state ein Dispatch-Latch ist, den beim Silent-Abort niemand
zuruecksetzt (nicht-heartbeatende Runtimes wie omp floaten ihn nie).
Der Skip war fuer heartbeatende Agents redundant (deren working-Heartbeat
refresht last_task_activity_at → Threshold greift nie) und fuer alle anderen
fatal. Entfernt; Regressionstest
``test_blocks_zombie_despite_run_state_running`` ersetzt den frueheren
Inverstest.
