# ADR-073 — Sessions-Chat-View: read-only Transkript-Tailing + tmux-Eingabe als zweite Surface

**Status:** Accepted
**Datum:** 2026-08-16
**Scope:** Backend/Services · Backend/API · Frontend/Pages · Infra/Runtime

## Kontext

Die `/sessions`-Seite zeigte bislang ausschliesslich den rohen tmux-Pane (xterm.js über
PTY-WebSocket) als einzige Möglichkeit, einer Agent-Session zu folgen. Das ist für einen
Menschen anstrengend zu lesen (Terminal-Scrollback, kein Markdown, keine strukturierte
Tool-Sicht) — der Operator wollte eine Chat-Ansicht im Stil von Codex/Claude-Code-Web:
volle Breite, einklappbare Tool-Zeilen, Approval-Karten, Composer mit Modell-/Context-
Anzeige.

Die zentrale Randbedingung: **die interaktive CLI in tmux darf nicht angetastet werden.**
Sie ist der produktive, tägliche Interaktionsweg (Operator UND Agent tippen/lesen dort),
mehrfach live gehärtet (ADR-071 Turn-Signale, Recycler, Pane-UI-Erkennung). Jede Lösung,
die die TUI ersetzt oder ihr Verhalten ändert, riskiert die gesamte Flotte.

Was macht Claude Code (der Docker-Harness aller 8 Worker-Agents plus Boss) bereits, das
sich zweitverwerten lässt? Es schreibt **live, Zeile für Zeile**, ein strukturiertes
JSONL-Transkript nach `$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<sessionId>.jsonl` —
User-/Assistant-Nachrichten, `tool_use`/`tool_result` (verknüpft über `toolu_*`-IDs),
Thinking-Blöcke, Subagent-Sidechains. `services/token_harvester.py` liest diese Dateien
bereits produktiv für Kosten-Attribution und kennt schon: Host-Pfad-Auflösung
(`_host_home()`), die Boss-Privacy-Heuristik (`_should_attribute_boss_path` — Boss'
`~/.claude` teilt sich mit den privaten Sessions des Operators), Line-Offset-Resume und die
Dedup-Regel (auf dem Top-Level-`uuid`-Feld, **nie** auf `message.id`, das über mehrere
Turns hinweg wiederholt wird). Das Transkript enthält **keine** Permission-Prompts,
Plan-Approval-Dialoge oder Menüs — die existieren nur im Pane.

## Entscheidung

**Die interaktive CLI bleibt unverändert.** Die Chat-Ansicht ist eine zweite,
unabhängige Lese-/Schreib-Surface über derselben tmux-Session:

- **Lesen:** ein Tailer (`services/transcript_chat.py::ChatTailerManager`) pollt das
  aktive Session-File (1 s-Intervall — macOS-Bind-Mount-mtime-Caching macht
  inotify/watchdog unzuverlässig, Poll ist das erprobte Muster aus `token_harvester`),
  parst neu angehängte Zeilen zu normalisierten Chat-Events (`message`/`tool`/
  `thinking`/`command`/`usage`/`state`/`session_changed`) und published sie auf
  `mc:agent:{id}:chat` (Redis Pub/Sub, `RedisKeys.agent_chat_channel`) → SSE-Endpoint
  `GET /agents/{id}/chat/stream`. Historie über `GET /agents/{id}/chat/history`
  (parst das File von vorn, Paging via `?before=<uuid>`).
- **Schreiben:** `POST /agents/{id}/chat/input` und `.../chat/keys` liefern Text bzw.
  benannte Tasten (`Escape`/`Enter`/Ziffern/…) über genau die Kanäle aus, die die
  Live-Terminal-Seite schon nutzt (`services/agent_chat_input.py`, gespiegelt aus
  `cli_terminal.py`): Docker-Agenten via `docker exec … tmux send-keys -t {slug}:0`,
  Boss (host) via Kurzverbindung zur host-pty-bridge (`ws://host.docker.internal:7682/`).
  Die CLI verarbeitet Text/Slash-Commands exakt wie eine getippte Eingabe — MC simuliert
  Tastatureingabe, es gibt keinen zweiten Eingabepfad in die CLI hinein.
- **Was das Transkript nicht hergibt** (Permission-Prompts, Menüs, Ready-Status) kommt
  aus einer separaten **Pane-State-Sonde** (`services/pane_state.py`): `tmux
  capture-pane -p` alle ~2 s (piggybacked auf den Tailer-Tick), Heuristik-Matching gegen
  bekannte Claude-Code-Prompt-Formen (nummerierte Optionen, Fragezeilen, Menü-Footer).
  Bewusst ein Heuristik-Klassifikator, kein Parser: kein Treffer heisst `unknown`, nie
  eine geratene Antwort — die Freigabe-Entscheidung bleibt in der CLI, MC genehmigt nie
  automatisch.
- **Adapter-Kontrakt statt Claude-Code-Spezialcode:** Diese vier Bausteine (Session-
  Resolution+Parser, Tailer, Eingabe-Kanal, Pane-State-Sonde) sind bewusst als
  austauschbare Einheit pro CLI gedacht, nicht als in `transcript_chat.py`
  fest verdrahtete Claude-Code-Logik — dieselbe Trennung wie der
  `HOST_ADAPTERS`-Kontrakt für Host-Harnesses (ADR-064) und der
  Vier-Funktionen-Kontrakt für Dispatch-Turn-Erkennung (ADR-071). v1 liefert nur den
  Claude-Code-Adapter (8 Docker-Agenten + Boss); ein zweiter Harness (Sparky/openclaude)
  liefert dieselben vier Bausteine, ohne den SSE-Kern/Frontend-Reducer anzufassen.
  Referenz-Kontrakt inkl. Pflicht-Discovery-Checkliste:
  `~/.claude/skills/mc-chat-cli-adapter/SKILL.md`.

## Alternativen

- **Agent-SDK-owned Sessions** (Claude Agent SDK treibt die Session statt der
  interaktiven TUI): würde die produktive, live gehärtete TUI ersetzen —
  Turn-Signale (ADR-071), Recycler-Verhalten, Pane-UI-Erkennung, jahrelang gegen echte
  CLI-Versionen gehärtete Fixtures wären alle hinfällig. Verworfen: zu grosser
  Blast-Radius für eine reine Darstellungs-Anforderung, und der Operator tippt/liest
  weiterhin regulär direkt in der TUI — ein Ersatz hätte zwei konkurrierende
  Wahrheiten über den Session-Zustand erzeugt.
- **ANSI-Terminal-Output parsen** (Pane-Bytes/Escape-Sequenzen in Chat-Nachrichten
  übersetzen): fragil gegen jede CLI-Formatierungsänderung (Box-Drawing, Spinner-Frames,
  Soft-Wrap), liefert keine strukturierten Tool-/Thinking-/Usage-Daten (nur Text), und
  hätte für jede Claude-Code-Version neu kalibriert werden müssen. Verworfen — genau der
  Fallback-Charakter, den die Pane-State-Sonde bewusst nur für die schmale Teilmenge
  „Menü/Prompt erkennen" übernimmt, nicht für die volle Chat-Darstellung.
- **Freies, draggable Panel-System** (Claude-Desktop-Stil) für Terminal/Diff/Browser:
  verworfen zugunsten fester Toggle-Panels (Icon-Rail) — v1-Scope-Reduktion, kein
  Architektur-Grund.

## Konsequenzen

### Positiv
- **Additiv, nahezu risikofrei:** neue Route + neue Endpoints, kein bestehender Pfad
  geändert. Rollback = PR revert, der bestehende Terminal-Pfad bleibt jederzeit ein
  Klick entfernt (Icon-Rail-Toggle) als Fallback/Wahrheitsquelle.
- **Wiederverwendung statt Neuerfindung:** Host-Pfad-Auflösung, Boss-Privacy-Filter und
  die uuid-Dedup-Regel kommen unverändert aus `token_harvester` statt einer zweiten,
  potenziell abweichenden Implementierung.
- **Truthful-Status-Prinzip:** jeder angezeigte Zustand stammt aus beobachteten Daten
  (Transkript-Wachstum, Pane-Sonde) — nie aus einer optimistischen Annahme nach dem
  Senden. Verhindert die häufigste Fehlerklasse bei Codex-artigen UIs (hängendes
  „arbeitet…", falsch feuernde Approvals).
- **Adapter-Kontrakt hält die Tür für weitere Harnesses offen**, ohne dass der SSE-Kern,
  der Redis-Kanal oder der Frontend-Reducer je CLI-spezifisches Wissen brauchen.

### Negativ
- **Die Pane-State-Sonde ist strukturell eine Heuristik**, kein Parser — ein
  Claude-Code-Update, das Menü-/Prompt-Layouts ändert, kann `permission_prompt` still
  brechen (ADR-071 hat für die Turn-Signal-Scraping-Schicht genau dieses Risiko bereits
  einmal real getroffen: claude-cli 2.1.x's bare `❯` mid-turn). v1 hat dafür noch
  keinen Golden-Fixture-TCK wie ADR-071 — nur Unit-Tests gegen aufgezeichnete
  Pane-Snapshots. Ein zukünftiger CLI-Versionssprung braucht denselben
  Fixture-Nachzieh-Prozess.
- **Recycler-Interaktion:** der Agent-Recycler killt idle Claude-Sessions alle
  ~5–8 Min basierend auf `~/.claude/last-task.marker`. Chat-Aktivität war dafür
  ursprünglich unsichtbar — eine laufende Chat-Konversation mit einem sonst idle Agenten
  wurde mitten im Gespräch gekillt (Live-Gate-Befund). Fix: `send_text` in
  `agent_chat_input.py` touched denselben Marker bei jeder Eingabe. Jeder künftige
  Eingabe-Kanal (neuer Harness, neuer Transport) muss diese Kopplung explizit
  mitdenken, sonst reproduziert er denselben Fund.
- **Fail-closed-Privacy ist ein hartes Korrektheitserfordernis, kein Nice-to-have:**
  Boss' Transkriptverzeichnis ist das persönliche `~/.claude` des Operators — `transcript_allowed()`
  lässt cli-bridge-Agenten immer durch (eigenes, isoliertes Workspace), gated Boss aber
  explizit auf `slug in _BOSS_SLUGS` + die `token_harvester`-Attributionsheuristik, und
  jeder Lesefehler (fehlende/unlesbare Datei) fällt auf **verweigert**, nie auf
  **erlaubt**. Jede künftige Änderung an Boss' Host-Setup (neuer Checkout-Pfad, neue
  CWD-Konvention) muss gegen diesen Filter geprüft werden — ein stiller Bruch würde
  private Sessions in die Chat-UI durchreichen.
- **Ein zusätzlicher Docker-exec-Roundtrip alle ~2 Sekunden** (die Pane-Sonde) pro
  offener Chat-Verbindung — refcounted (`ChatTailerManager.acquire`/`release`), damit N
  Browser-Tabs auf demselben Agenten nur einen Poller erzeugen, aber bei vielen
  gleichzeitig geöffneten Chats ist das zusätzliche Docker-Last, die die reine
  Terminal-Ansicht nicht hatte. Der Tailer-Poll selbst (1 s) ist dagegen ein reiner
  lokaler Datei-Read, kein Docker-exec.
- **Statusline-Hook ist ein flottenweiter Single-Source-of-Truth-Pfad (ADR-006):** die
  Context-Window-Wahrheit im Composer kommt aus `docker/shared/statusline-mc.sh`, das
  `plugin_manager.render_agent_settings` in jeden claude-harness-Agenten ausrollt — ein
  `settings.json`-Key (`statusLine`) plus eine Kopie des Skripts selbst
  (`cli_agent_settings.json.j2`), Stand Ledger 7/7 Agenten. Künftige Änderungen an
  Skript oder Key müssen über diesen Template-Pfad laufen, nicht per Hand-Edit im
  Container, sonst driftet die Flotte.
- **Effort-Umstellung aus dem Chat ist NICHT session-lokal, sondern dauerhaft:**
  `POST /agents/{id}/chat/effort` fährt `/effort <level>` in die TUI, und Claude Code
  2.1.233 kennt dafür keinen session-only Pfad — auch die „s"-Option des
  `/model`-Pickers scopet nur das MODELL auf die Session, nicht den Effort. Jede
  Umstellung schreibt damit den **persistierten Standard** des Agenten in seiner
  `settings.json` um und gilt für jede frische Session, bis sie erneut umgestellt wird
  oder der nächste `sync-config`/Reprovision sie aus dem Template überschreibt (ADR-006:
  DB → Template → Datei bleibt die Single Source of Truth, ein Chat-Wechsel ist
  ausdrücklich KEINE Eintragung in diese Kette). Empirisch verifiziert, nicht aus der
  Doku gelesen. Konsequenzen: (1) die UI muss es sagen — das Effort-Dropdown im Composer
  trägt die Zeile „Gilt als neuer Standard des Agenten."; (2) wer Effort künftig
  wirklich session-lokal braucht, braucht CLI-Support, keinen UI-Trick; (3) ein
  Reprovision kann eine per Chat gesetzte Stufe stillschweigend zurücksetzen — das ist
  gewollt, aber überrascht, wenn man es nicht weiß.
- **v1-Scope-Lücke bewusst in Kauf genommen:** Hermes/Jarvis/Sparky haben (noch) keinen
  Adapter — ehrlicher „kein Transkript verfügbar"-Zustand statt eines vorgetäuschten
  Chats. Sparky (openclaude-Dialekt) ist der nächstliegende v2-Kandidat, weil
  `token_harvester` einen Teil seines Transkript-Dialekts schon kennt.

### Härtungs-Regeln, gegen die künftige Änderungen geprüft werden müssen (aus dem
Live-Gate teuer bezahlt, siehe `.superpowers/sdd/2026-08-13-sessions-chat-view-plan/progress.md`)
- **Tailer-Offset wird bei `acquire()` synchron vor dem Task-Start ermittelt/gesetzt**
  (nicht als erste Zeile von `_run()`), sonst würde ein First-Connect das gesamte
  bestehende Transkript nochmal als „live" Events durchlaufen lassen, obwohl
  `/chat/history` es bereits geliefert hat.
- **Binäre Byte-Reads mit expliziten Offsets** (`_read_new_chunk`), nicht Text-Modus —
  ein mehrbyte-UTF-8-Zeichen, das über zwei Polls hinweg geteilt wird, darf weder
  doppelt gezählt noch abgeschnitten werden; dekodiert wird erst nach dem
  Zeilen-Puffern.
- **Dedup auf dem Top-Level-`uuid`**, sowohl in `read_history` als auch live im Tailer
  (`_peek_uuid`) — Claude Code kann eine Zeile nach einem Resume identisch wiederholen.
- **Getrennter Enter-Frame pro Transport, nie im selben Frame wie der Text:** Der
  Docker-Pfad sendet Text (`tmux send-keys -l --`) und `Enter` als zwei separate
  `docker exec`-Aufrufe (ein fehlender zweiter Aufruf liess Nachrichten unversendet in
  der Eingabebox liegen). Der Boss-WS-Pfad sendet Text und `\r` als zwei eigene
  WebSocket-Frames mit 150 ms Pause dazwischen — Text+Enter in einem Frame lässt die
  Claude-TUI-Paste-Erkennung den Enter als Teil des eingefügten Texts schlucken statt
  ihn abzuschicken (live reproduziert: Nachricht sass stundenlang unversendet).

## Referenzen

- Betroffene Dateien: `backend/app/services/transcript_chat.py`,
  `backend/app/services/pane_state.py`, `backend/app/services/agent_chat_input.py`,
  `backend/app/routers/agent_chat.py`, `backend/app/services/workspace_diff.py`,
  `frontend-v2/src/components/chat/*`, `frontend-v2/src/app/sessions/page.tsx`,
  `frontend-v2/src/lib/chatTypes.ts`, `frontend-v2/src/hooks/useChatStream.ts`
- Design-Doc: `docs/plans/2026-08-13-sessions-chat-view-design.md` (lokal, gitignored)
- Umsetzungs-Ledger: `.superpowers/sdd/2026-08-13-sessions-chat-view-plan/progress.md`
- Adapter-Onboarding: `~/.claude/skills/mc-chat-cli-adapter/SKILL.md`
- Verwandte ADRs: ADR-071 (Adapter-Kontrakt + Golden-Fixture-TCK — dasselbe
  Signal-Hierarchie-/Fixture-Muster, hier noch ohne TCK), ADR-064
  (`HOST_ADAPTERS`-Registry — dasselbe Protocol+Registry-Muster für Harness-Vielfalt),
  ADR-039 (OpenClaw Gateway Sunset — der poll-basierte Dispatch-Pfad bleibt von dieser
  zweiten, unabhängigen Chat-Surface komplett unberührt)
