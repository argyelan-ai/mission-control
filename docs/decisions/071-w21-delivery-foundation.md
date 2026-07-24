# ADR-071 — W2.1 Delivery Foundation: native turn signals, pull-based delivery, adapter TCK

**Status:** Accepted
**Datum:** 2026-07-22
**Scope:** Infra/Runtime · Backend/Dispatch · Backend/Tests

## Kontext

Die Zustellung an CLI-Bridge-Agents hing an zwei fragilen Säulen: (1) die
Turn-Erkennung (idle/working/crashed) las ausschliesslich `tmux capture-pane`
und parste Prompt-Glyphen/Spinner, (2) Nudges/Recovery wurden teils gepusht. Als
claude-cli 2.1 erschien, brach es **alle** Scraping-Heuristiken gleichzeitig —
NBSP-Prompt (U+00A0), neue Spinner, Collapse-Chips, weggefallene Box-Glyphs — und
verursachte 6 Prod-Bugs, die live an der laufenden Flotte geflickt wurden
(Messlauf 2026-07-20). Es gab keinen Mechanismus, ein CLI-Update *vor* dem
Rollout zu prüfen: jede neue oder aktualisierte CLI war Blindflug.

Anforderungen: (a) Turn-Erkennung soll nicht mehr allein am Pane-Scraping hängen;
(b) Zustellung soll pull-basiert und idempotent sein; (c) ein neues/aktualisiertes
CLI muss schnell und sicher anbindbar sein, mit einem Regressions-Netz, das den
nächsten CLI-Bruch als roten Test statt als kaputte Flotte zeigt.

## Entscheidung

Ein dreistufiges „Delivery Foundation"-Fundament, alles hinter der bestehenden
Bridge-Mechanik, byte-identische Flotte:

- **Phase A — native Turn-Signale.** claude-code-Hooks (`UserPromptSubmit`/`Stop`)
  appenden `<epoch> submit|stop` an `~/.turn-signal`. `detect_turn_state` liest
  diese Datei zuerst (`TURN_SIGNAL_MODE=auto`) und fällt nur bei fehlender/
  veralteter Datei aufs Pane-Scraping zurück. Scraping bleibt PFLICHT-Fallback,
  weil `Stop` bei Interrupt (Esc) und mid-turn-API-Crash nicht feuert — `crashed`
  bleibt scrape-autoritativ.
- **Phase B — pull-basierte Zustellung.** `mc inbox`-Nudge + Pull-Delivery: der
  Agent zieht Dispatches/Nudges über den Poll-Pfad, statt sie gepusht zu bekommen.
- **Phase C — Adapter-Kontrakt + Golden-Fixture-TCK.** Ein 4-Funktionen-Kontrakt
  (`detect_turn_state` / `paste_and_submit` / `verify_paste` / `detect_pane_ui`)
  mit einer Signal-Hierarchie (1. natives Signal/Hook → 2. strukturierter Output
  → 3. Pane-Scraping+Fixtures). Reale Pane-Snapshots pro CLI unter
  `backend/tests/fixtures/panes/<cli>/` pinnen die Scraping-Schicht; die
  parametrisierte Conformance-Suite `backend/tests/test_adapter_tck.py` läuft für
  **jedes** Fixture-Verzeichnis automatisch mit (neues CLI = Fixtures aufnehmen,
  Suite läuft mit). Ein Recorder `tools/record-pane-fixtures.sh` nimmt die
  Fixtures live aus laufenden Containern auf. Ein Byte-Identitäts-Guard verhindert
  Drift der zwei handgepflegten Lib-Kopien. Vollständiger Kontrakt +
  Onboarding-Checkliste: `docs/adapters.md`.

## Alternativen

- **Nur Scraping härten (kein Signal):** Jede CLI-Version hätte weiter live
  geflickt werden müssen — genau der Schmerz, den wir beseitigen. Verworfen.
- **Push-only-Delivery beibehalten:** Kein at-least-once, keine Idempotenz,
  Race-anfällig bei Session-Reset. Verworfen zugunsten Pull.
- **Signal statt Scraping (Scraping ganz ersetzen):** Unmöglich — `Stop` feuert
  nicht bei Interrupt/Crash. Scraping bleibt Fallback, kein Ersatz.
- **TCK gegen synthetische Pane-Strings (kein Live-Recording):** Genau das hat
  die 2.1-Brüche verpasst (die Tests kannten die neuen Glyphs nicht). Reale,
  aufgenommene Golden-Fixtures sind der Kern des Werts. Verworfen.

## Konsequenzen

### Positiv
- Turn-Erkennung ist im Normalfall deterministisch (Hook-Signal), nicht mehr
  raten am Pane.
- Ein CLI-Update wird gegen echte Golden-Fixtures geprüft, bevor es die Flotte
  trifft — der nächste Bruch ist ein roter Test.
- Ein neues CLI anbinden = Fixtures aufnehmen; die TCK parametrisiert sich selbst.
- Byte-Identitäts-Guard fängt Split-Brain der zwei handgepflegten Lib-Kopien.

### Negativ
- Zwei Lib-Kopien bleiben handgepflegt (build-agent-images.sh synct nur poll.sh)
  — der Guard mildert, beseitigt aber nicht die Doppelpflege.
- Die TCK deckte sofort eine reale Scraping-Lücke auf: claude-cli 2.1.x rendert
  die Eingabebox mit bare `❯` auch mitten im Turn, sodass der Idle-Check im
  Scrape-Modus einen aktiven Turn als `idle` klassifiziert. In Produktion vom
  Phase-A-Signal maskiert. Als `xfail(strict)` (`claude/working`) kodiert — Fix
  gehört in `turn-state.sh` (ausserhalb des TCK-Scopes). Beim Fix flippt der
  xfail auf XPASS und erzwingt Entfernen des Eintrags.
- `detect_pane_ui` kann claude 2.1.x und openclaude in bare Panes nicht
  unterscheiden (keine Box-Glyphs mehr) — `PANE_UI_OVERRIDE` (ins Image gebacken)
  bleibt der verlässliche Pfad; die TCK testet beide (Heuristik-Golden + Override).

## Referenzen

- Betroffene Dateien: `docker/mc-agent-base/lib/turn-state.sh`,
  `docker/mc-claude-agent/lib/turn-state.sh`, `docker/*/lib/ui-detect.sh`,
  `docker/*/lib/paste-verify.sh`, `docker/shared/poll.sh`,
  `tools/record-pane-fixtures.sh`, `backend/tests/test_adapter_tck.py`,
  `backend/tests/fixtures/panes/`, `docs/adapters.md`
- Commits: Phase A `e2a02dbf`, Phase B `3a9be6be`
- Verwandte ADRs: ADR-064 (Host-Harness-Adapter), ADR-068 (Grok TUI paste model)
- Kontext: Interaktionsmodell 2.0 / comm_v2 Messlauf 2026-07-20
