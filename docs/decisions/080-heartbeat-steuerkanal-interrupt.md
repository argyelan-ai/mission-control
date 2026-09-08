# ADR-080 — Heartbeat-Steuerkanal: laufende omp-Zuege extern unterbrechen (Kind.INTERRUPTED)

**Status:** Accepted
**Datum:** 2026-09-07
**Scope:** Backend/Dispatch + Infra/Runtime (omp-bridge)

## Kontext

Ein laufender omp-Zug war von aussen nicht unterbrechbar. Stop-Knopf,
`mc blocked` eines Leads und blocker/handoff-Kommentare werden erst an
Turn-Grenzen zugestellt (`serve_loop` -> `_deliver_at_boundary`); waehrend
`_observe_native_turn` blockt der Loop. Vorfall 07.09.: 16+ Minuten.
Der Heartbeat (alle 30 s, Daemon-Thread) ist der einzige offene Kanal
mitten im Zug — bisher wurde seine Antwort verworfen.

Liveprobe (isolierte Test-omp-Instanz, omp v18.1.10):
- `Escape` mitten im Tool -> sofort `turn_end stopReason=aborted`
  ("Interrupted by user") + `agent_end` im HOOK-SIGNAL, Prozess lebt weiter.
- `C-c` mitten im Tool -> wirkungslos (nur Tool-PTY).
- Kein rpc/abort-Endpunkt in der CLI (`--mode=rpc` = Ausgabeformat,
  `omp acp` = eigener Prozess).

## Entscheidung

Die Heartbeat-Antwort wird zum Steuerkanal. `POST /agent/me/heartbeat`
liefert optional `control = {"interrupt": "hard"|"soft", "reason": str}`:

- `hard` — `run_control == "stopped"` ODER Task `blocked` durch
  Fremdakteur (neuester blocker/handoff-Kommentar der Block-Episode
  nicht vom Agent selbst authored).
- `soft` — ungelesene blocker/handoff-Kommentare jenseits des
  Comment-Cursors des Agenten.
- Beides gleichzeitig -> `hard`. Antwort ohne `control` = Legacy.

Die Bridge (`start_heartbeater` `_on_control`) setzt daraus ein
thread-safe `InterruptState`; `_observe_native_turn` prueft es in jeder
Runde und faehrt bei Signal die Abbruch-Leiter: (1) Protokoll-Abbruch
entfaellt (kein Kanal), (2) `Escape` + Wartezeit `OMP_INTERRUPT_GRACE`
(Default 20 s) auf das HOOK-SIGNAL — nie Pane-Lesen, (3) `C-c`, gleiche
Wartezeit, (4) bestehender Watchdog-Kill + Relaunch. Neuer Ausgang
`Kind.INTERRUPTED`: `decide_lifecycle` -> `halted_interrupted` — kein
Retry, keine Eskalation, kein `mc finish`, kein "omp abort (hang)".
Danach Leerlauf -> Poll -> `stopped`/`blocked` -> Nudge. `soft` liefert
den Nudge am Turn-Boundary und setzt im selben Session-Kontext fort.

## Alternativen

- **Pane-Scraping des Stop-Zustands** → Verworfen: das HOOK-SIGNAL ist die
  alleinige Wahrheitsquelle (ADR-049); Pane-Text ist nicht parsebar
  stabil und das Contract verbietet Pane-Lesen beim Warten auf `turn_end`.
- **Neuer Backend-Endpoint `POST .../interrupt`** → Verworfen: Guardrail
  "keine neuen Endpoints"; der Heartbeat ist bereits der offene Kanal.
- **Nur Watchdog-Kill ohne Leiter** → Verworfen: SIGKILL verliert die
  Session; `Escape` beendet den Zug sauber und der Prozess lebt weiter
  (Session-ID bleibt fuer `soft`-Fortsetzung erhalten).
- **Signal via tmux send-keys in die Session injizieren** (Chat-Nachricht
  "STOPP") → Verworfen: hängt vom Composer-Zustand ab, nicht deterministisch,
  und der Agent könnte es ignorieren/mitten im Tool nicht lesen.

## Konsequenzen

### Positiv
- Laufende Zuege enden bei Stop in < 45 s praktisch (Heartbeat-Intervall
  + Grace statt Laufzeit-Ende).
- Sabotage-sicher: Backend ohne `control`-Feld -> Bridge verhaelt sich
  byte-identisch wie heute (Legacy-Pfad getestet).
- `Kind.INTERRUPTED` ist nie `abort_hang`: kein Fehl-Retry, keine
  Blocker-Eskalation, kein Fehl-Alarm im Thread.
- poll.sh-Agenten (claude-code) bleiben unveraendert.

### Negativ
- Interrupts kommen mit bis zu einem Heartbeat-Intervall (30 s) Verzoegerung.
- Die Leiter kann bei haengendem TUI bis ~2x Grace + Watchdog dauern
  (Escape -> C-c -> Kill).
- Neue Kopplung Backend-Antwort <-> Bridge-Callback (durch Schema-Feld
  und Tests auf beiden Seiten abgedeckt: `test_heartbeat_control.py`,
  `tests/test_interrupt.py`).
