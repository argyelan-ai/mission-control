# ADR-068 — Grok bridge v2: TUI paste model (fleet-uniform, no `-p`)

**Status:** Accepted
**Datum:** 2026-07-11
**Scope:** Infra/Host-Bridge, Backend/Provisioning, Backend/Runtime
**Supersedes (nur Delivery-Modell):** ADR-066 §1 (headless Subprocess-Delivery)

> **067** ist von der Wizard-Session für den launchctl-Host-Helper reserviert; dieses
> ADR ist **068**.

## Kontext

ADR-066 hat grok als host-side Harness integriert und dabei ein **headless**
Delivery-Modell gewählt: pro Dispatch ein one-shot `grok -p --output-format
streaming-json`-Subprozess, dessen NDJSON-Stream die Bridge reduziert, wobei die
**Bridge** den Lifecycle (`ack`/`finish`/`blocked`) besitzt.

Mark hat danach eine **harte, fleet-weite Regel** gesetzt: der CLI-Print-Mode `-p`
(headless single-turn) ist **verboten**. Zwei Gründe:

1. **Kosten** — bei Claude Code erzeugt der Print-Mode Extrakosten; Mark will diese
   Klasse von Aufrufen nirgends in der Fleet.
2. **Einheitlichkeit** — jeder andere Agent (claude-cli via poll.sh, Hermes via
   hermes-bridge) läuft als **persistente interaktive TUI**, in die Dispatches
   gepastet werden und die ihren MC-Lifecycle **selbst** treibt. Ein Sonder-Delivery
   nur für grok bricht dieses Muster.

Die gestern gebaute headless grok-bridge v1 wurde deshalb stillgelegt.

## Entscheidung

Das grok-Delivery-Modell wird auf das **Hermes-/poll.sh-Muster** umgestellt (paste
model). ADR-066 bleibt im Übrigen gültig — **nur §1 (Delivery)** wird ersetzt;
Adapter (`GrokAdapter`), Protokoll-Einordnung (`HARNESS_PROTOCOLS["grok"]`), die
Display-Anker-Runtime `grok-cloud` und der Provisioning-Dispatch bleiben unverändert.

### 1. `scripts/grok-bridge.py` — persistente TUI + Poll→Paste

Eine **einzelne** `grok`-TUI läuft in einer tmux-Session (`grok`, Slug-Konvention),
gestartet als `grok --no-alt-screen --permission-mode acceptEdits` im Task-Workspace
(`~/.mc/workspaces/grok`). Die Bridge:

```
GET /api/v1/agent/me/poll  (CLAIM)
  state=new_task →
    deliver_task_context(task):  /tmp/mc-context.env  (TASK_ID/BOARD_ID/ATTEMPT_ID)
                                 + tmux set-environment (belt-and-suspenders)
    paste_and_submit(build_dispatch_prompt(task))   # load-buffer → paste-buffer
                                                     # → bracketed-paste-end → Enter
    _mark_active(task)  # arm the no-progress watchdog
  idle/cancelled/stopped → dedup-cache + active-task tracking leeren
```

- **Kein `-p`, kein `--prompt-file`, kein streaming-json** — nirgends. `grok` läuft
  ausschliesslich als interaktive TUI.
- **Agent-getriebener Lifecycle** (Kehrtwende gegen ADR-066): der grok-Agent ruft
  **selbst** `mc ack` (sofort), `mc comment progress`, `mc deliverable` und schliesst
  **selbst** via `mc finish --review` / `mc blocked` — exakt wie jeder claude/hermes
  Host-Agent. Die **Bridge schliesst keine Tasks mehr**. Der `mc`-CLI liest seinen
  Kontext aus `/tmp/mc-context.env` (`from_env`, Datei gewinnt), das die Bridge
  **vor** jedem Paste schreibt.
- **Session-Autostart** — fehlt die tmux-Session, startet die Bridge sie selbst
  (`tmux new-session -d -s grok -c <workspace> 'grok --no-alt-screen …'`) und wartet
  per Pane-Capture auf den Prompt-Glyph `❯` (`wait_for_agent_healthy`).
- **No-Progress-Watchdog** (Pflicht statt eines Lifecycle-Timeouts, weil der Agent
  den Endzustand besitzt): ein Thread vergleicht periodisch das Pane-Capture; ändert
  es sich über `GROK_NUDGE_IDLE_TIMEOUT` (300s) nicht, während ein Task aktiv ist,
  pastet die Bridge einen **Nudge** (Erinnerung an `mc finish`/`mc blocked`),
  gedeckelt auf `GROK_NUDGE_MAX` (2). Der Watchdog **blockiert/finished nie** selbst —
  er un-sticked nur einen still hängenden Turn, damit nichts unsichtbar in_progress
  bleibt.
- **Dedup** via `(task_id, attempt_id)` — `/me/poll` liefert `state=new_task` bis der
  Agent `mc ack` ruft, also wird nur bei neuem Task **oder** neuer Attempt-ID
  neu-gepastet (poll.sh-Muster).
- **Heartbeat** nur bei laufender tmux-Session (die Session **ist** die Liveness-
  Quelle → eine tote Session wird nach 90s korrekt stale). SIGTERM → sauberer Exit 0
  (launchd `KeepAlive` restartet nicht bei absichtlichem Stop); Crash → `[fatal]` +
  `SystemExit(1)`. Port **18795**, Bind **127.0.0.1** only.
- **HTTP-Control:** `GET /health` (+`session`/`tmux_running`), `POST /start`
  (Session hochziehen), `POST /restart` (kill+restart → re-sourct `agent.env`),
  `POST /stop` (Escape in die TUI, kill**t** die Session nicht).

### 2. Sessions-Seite mountet das grok-Terminal

`bootstrap_grok_agent` gibt jetzt `tmux_session="grok"` zurück (nicht mehr `None`),
und `cli_terminal._HOST_AGENT_TMUX_TARGETS["grok"] = {session:"grok", socket:<user-
default>}` — so mountet die Sessions-Seite das grok-Terminal über denselben
host-pty-Pfad wie Hermes. (Vorher war grok headless und hatte keine mountbare
Session.)

### 3. Was aus ADR-066 gültig bleibt

Adapter/Protokoll/Runtime/Provisioning-Dispatch sind **unverändert**: `GrokAdapter`
(`harness="grok"`, `protocol="grok"`, nur MC_*-Env), `HARNESS_PROTOCOLS["grok"]={"grok"}`,
Seed-Runtime `grok-cloud` als Display-Anker, `is_compatible()`-Gate, `reload` =
launchctl kickstart des `com.mc.grok-bridge.plist`. Es ändert sich **nur**, wie ein
Dispatch geliefert wird und wer den Lifecycle besitzt.

## Alternativen

- **v1 headless behalten.** Verworfen — verstösst gegen Marks fleet-weites `-p`-Verbot
  (Kosten + Einheitlichkeit).
- **Bridge behält den Lifecycle auch im TUI-Modell** (Turn-End-Erkennung per Hook wie
  omp-native). Verworfen für v1 — grok build hat (Stand v0.2.93) keinen stabilen
  Turn-End-Hook, den wir wie omp anzapfen; das poll.sh-Muster (Agent ruft `mc`
  selbst + Watchdog-Nudge) ist bewährt und fleet-uniform. Falls grok später einen
  Turn-End-Hook bietet, ist eine deterministische Lifecycle-Erkennung ein additives
  Upgrade.

## Konsequenzen

### Positiv

- **Fleet-uniform** — grok liefert/lebt exakt wie claude/hermes; kein Sonder-Delivery,
  kein verbotener `-p`.
- **Mountbares Terminal** — die grok-TUI ist auf der Sessions-Seite sicht- und
  bedienbar (vorher headless/blind).
- **Hang-sicher ohne Bridge-Lifecycle** — der No-Progress-Nudge un-sticked stille
  Turns; der Agent besitzt den sauberen Endzustand wie überall sonst.

### Negativ

- **Lifecycle-Garantie ist jetzt agent-getrieben** — ein Agent, der weder `mc finish`
  noch `mc blocked` ruft und trotzdem Pane-Aktivität zeigt, kann prinzipiell länger
  in_progress bleiben als im deterministischen v1-Modell. Mitigation: der
  Dispatch-Prompt ist explizit + der Watchdog-Nudge; dasselbe Restrisiko trägt die
  gesamte poll.sh-Fleet.
- **Kein Live-Provisioning in dieser Runde** — Bridge/Tests sind gebaut und grün, der
  echte `POST /provision` + ein voller grok-Lauf gegen das Abo bleiben Marks Gate
  (Rate-Limit-Schonung; nur ein einziger manueller Smoke).

## Referenzen

- Betroffene Dateien:
  - `scripts/grok-bridge.py` (v2, TUI paste model — Subprocess-/Reducer-Pfade entfernt)
  - `backend/app/services/agent_bootstrap.py` (`GROK_TMUX_SESSION`, `tmux_session="grok"`)
  - `backend/app/routers/cli_terminal.py` (`_HOST_AGENT_TMUX_TARGETS["grok"]`)
  - `docker/grok/com.mc.grok-bridge.plist` (Kommentar: TUI-Modell)
  - Tests: `backend/tests/test_grok_bridge.py` (paste-Mechanik, Autostart, Watchdog),
    `test_grok_provisioning.py` (`tmux_session=="grok"`)
- Verwandte ADRs: **ADR-066** (grok host harness — supersedet nur das Delivery-Modell),
  **ADR-029** (Hermes host-side tmux-Worker — Delivery-Vorbild), **ADR-049** (omp
  native TUI — Pane-Capture/Ready-Muster), **ADR-064** (HostHarnessAdapter — bleibt).
- Externe Quellen: xAI Grok Build CLI v0.2.93 (`grok --help`: `--no-alt-screen`,
  `--permission-mode`, `--minimal`).
