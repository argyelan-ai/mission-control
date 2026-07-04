# ADR-051: Loops — ergebnisgesteuerte Task-Schleifen (Meta-Controller über Tasks)

**Status:** Accepted (2026-07-04) · L1 umgesetzt, L2–L4 geplant

## Kontext

MC hat zeitgesteuerte Wiederholung (Schedules: WANN) und deterministische
Schrittfolgen (Workflows: WIE), aber keine **ergebnisgesteuerte** Wiederholung
(BIS): „Arbeite dieses Backlog ab, Runde für Runde, bis das Ziel erreicht ist,
das Budget aufgebraucht ist oder ein Gate stoppt." Schedules feuern unabhängig
vom Ergebnis; Workflows laufen an der Task-Pipeline vorbei und sind seit dem
Gateway-Sunset funktional tot.

## Entscheidung (Marks 4 Entscheide, 04.07.)

1. **Loop = Meta-Controller über normale Tasks.** Ein Loop führt selbst nichts
   aus: Pro Runde erzeugt der Loop-Runner einen normalen Parent-Task
   (`create_task_internal`, KEIN assigned_agent_id → Board-Lead-first) und
   lässt die bestehende Maschinerie arbeiten — ACK, Watchdog, Review-Pflicht,
   Approvals. Der Workflow-Fehler (eigener Ausführungspfad) wird explizit
   nicht wiederholt.
2. **Default-Gate = nur bei Problemen/Merges.** `human_every_n_rounds` Default
   0 (nie); Merges/destruktive Aktionen gated die Task-Pipeline selbst.
   Eskalation via neuem Approval-`action_type="loop_gate"` (Telegram-
   Quick-Resolve läuft generisch mit).
3. **L1 zuerst** (dieses ADR): `loops`+`loop_rounds` (Migration 0138),
   `LoopRunnerService`, `loop_gate`, minimale `/loops`-Seite, Runden-/
   Zeitbudget. Token/USD-Budget = L3 (setzt cost_collector-Revival voraus).
4. **Bau nach dem Repos-Merge** (Migration 0138 nach 0137).

## Mechanik (L1)

**Runner** (`services/loop_runner.py`, Singleton neben Scheduler/Watchdog,
30s-Tick, Per-Cycle-Redis-Lock `mc:loop_runner:cycle_lock`):

```
running-Loop ohne laufende Runde → Runde starten (Parent-Task + LoopRound)
running-Loop mit laufender Runde → warten bis Task terminal (done/failed)
Runde terminal → auswerten: LoopRound.outcome + Report (Reflexion, Deliverables)
  1. Circuit-Breaker: consecutive_failed_rounds ≥ pause_on_failed_rounds (Def. 2)
     → status=paused + loop_gate(circuit_breaker) + Telegram
  2. Stop: „BACKLOG LEER"/„ZIEL ERREICHT" in der Reflexion (stop_on_backlog_empty)
     · max_rounds · max_duration_minutes (an Rundengrenzen geprüft)
     → status=done
  3. Gate: human_every_n_rounds fällig → status=waiting_gate + loop_gate
  4. sonst → nächste Runde (Brief = Ziel + Backlog + Reports der letzten 3 Runden)
```

**loop_gate-Resolve** (`routers/approvals.py`): approved → `running` +
Fehlerserie zurückgesetzt (Runner startet nächste Runde); rejected → `paused`.
Operator-Aktionen via UI (`start/pause/stop`) superseden offene loop_gates.

**Leitplanken:** 1 aktiver Loop pro Board (409 beim Start) · jede Runde volle
Pipeline-Gates · Runden-Report Pflichtdisziplin (auf LoopRound persistiert,
fliesst in den nächsten Brief) · gelöschter Runden-Task = Fehlrunde
(Task-Delete-Endpoints lösen die nullable Loop-FKs).

**backlog_source:** `markdown` (eingebettete Liste, Pflichtfeld) · `open_ended`
(Lead findet das nächste Item selbst) · `project` (offene Projekt-Tasks,
Brief-Anweisung; harte Abfrage = L2) · `tag` (L2, verhält sich bis dahin wie
open_ended).

## Alternativen

- **Loop als Workflow-Kind:** verworfen — Workflows umgehen die Task-Pipeline
  (toter Pfad seit Phase 29).
- **Loop als Schedule-Variante:** verworfen — Schedules sind zeit-, nicht
  ergebnisgesteuert; Ergebnisauswertung/Circuit-Breaker passen nicht ins Modell.
- **Gate jede Runde als Default:** verworfen (Marks Entscheid 2) — Autonomie
  ist der Zweck; Probleme eskalieren ohnehin.

## Konsequenzen

- Runden sind voll inspizierbar (jede Runde = Parent-Task mit Kommentaren,
  Deliverables, Git) — `/loops` verlinkt nur dorthin.
- max_duration wird an Rundengrenzen geprüft; eine hängende Runde fängt der
  Task-Watchdog, nicht der Loop-Runner.
- L2: Telegram-Runden-Reports, Schedule-Trigger, project/tag hart, Gate-UI.
  L3: cost_collector-Revival → Token-/USD-Budget. L4: Runden-Lessons in die
  Knowledge Base.
