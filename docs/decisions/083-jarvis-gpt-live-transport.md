# ADR-083 — Jarvis GPT-Live-Transport (vorab, LiveKit-PR #7212)

**Status:** Accepted — PRODUKTIV seit 10.09.2026 (Marks Entscheid: GPT-Live ersetzt Realtime
für Jarvis, kein Nebenläufer/Test-Worker). `mission-control-voice-worker-1` läuft mit
`VOICE_API=live`/`VOICE_MODEL=gpt-live-1` auf dem Image, das dieses ADR beschreibt. Weiterhin
"vorab", weil LiveKit-PR #7212 noch offen ist (siehe Aufräumen unten).
**Datum:** 2026-09-10
**Scope:** Infra/Runtime (voice_worker) | Backend/Voice

## Rückweg (falls gpt-live-1/das Vorab-Plugin Probleme macht)

1. `.env` (Symlink `~/.mc/secrets/mission-control/.env`, gemountet über
   `.claude/worktrees/deploy-main`): `VOICE_API` entfernen oder auf `realtime` setzen,
   `VOICE_MODEL` zurück auf `gpt-realtime-2.1`, `VOICE_PROVIDER=openai` (unverändert).
   Vor-Änderungs-Stand live geprüft am 10.09.2026, bevor irgendetwas geändert wurde:
   `VOICE_PROVIDER=openai`, `VOICE_MODEL=gpt-realtime-2.1`, `VOICE_API` war noch nicht
   vorhanden (Feature existierte nicht).
2. Image zurück auf `mission-control-voice-worker:realtime-backup-20260910` (getaggter
   Snapshot des vorherigen, unveränderten `voice_worker/Dockerfile`-Builds, livekit-agents
   `1.6.7`).
3. `docker compose -p mission-control --env-file .env up -d --force-recreate voice-worker`
   aus `.claude/worktrees/deploy-main`.
4. Wirk-Beweis wie beim Rollout: Worker-Log zeigt sauberen Start ohne GPTLiveModel, ein Anruf
   funktioniert wieder über OpenAI Realtime.

## Kontext

Mark will Jarvis (den Voice-Worker, LiveKit Agent) auf OpenAIs neues Full-Duplex-Voice-Modell
`gpt-live-1` umstellen ("Live API", getrennt von OpenAIs bisheriger "Realtime API"). Live API läuft
ueber `wss://api.openai.com/v1/live/sessions` mit einem eigenen Event-Protokoll
(`session.start`/`session.started`/`session.input_audio.append`/`session.output_audio.delta`, PCM16
24kHz mono). Anders als bei Realtime ist das Voice-Modell strikt getrennt vom Denk-/Tool-Backend —
Reasoning + Tool-Calls muessen irgendwo anders passieren ("Delegation").

LiveKit (unser Agent-Framework, `livekit-agents`) hat den Adapter noch nicht released: PR #7212
("feat: add openai gpt-live duplex support", Repo `livekit/agents`) ist am 10.09.2026 offen, aufgesetzt
auf PR #6677 (Duplex-Adapter-Core). Weder `livekit-agents` core noch `livekit-plugins-openai` haben
`GPTLiveModel`/`llm.DuplexModel` auf PyPI — die Klasse existiert nur im PR-Branch (Head-SHA
`de3c5ce66058c6ab437ad41f963cbaeb39046c6d`, geprueft via `gh pr view 7212 -R livekit/agents --json
headRefOid`).

Jarvis' Denken/Tools (create_task, dispatch_to_agent, query_memory, …, ~20 `@function_tool`-Methoden)
leben in `jarvis_core/` und sind mit dem Telegram-Kanal geteilt (ADR-061). Das PR bietet zwei
Delegations-Modi an:

- **`delegation="responses"`** — ein Backend-Responses-Modell ruft die `@function_tool`-Methoden
  GENAU wie heute bei `RealtimeModel`. Kein Umbau der Tools noetig.
- **`delegation="client"`** — die Anwendung bekommt die Arbeit als `delegation_created`-Event und muss
  sie SELBST treiben (eigene `llm.LLM`-Instanz, eigener Tool-Loop, `append_commentary()` fuer die
  Antwort). Die Agent-Instanz darf dabei GAR KEINE `@function_tool` tragen — das Passieren eines Tools
  wirft `RealtimeError` beim Session-Start (siehe PR-Beispiel `client_delegation.py`).

## Entscheidung

1. **Transport-Wahl per `VOICE_API` env var** (`voice_worker/main.py`): `"realtime"` (Default,
   unveraendertes Verhalten) oder `"live"` (`_build_live_model()` → `GPTLiveModel(delegation="responses",
   ...)`). Import von `GPTLiveModel` ist in einen `try/except ImportError` gewrappt — fehlt das
   Plugin (Produktions-Image), faellt `VOICE_API=live` mit einer lauten Warnung auf `realtime`
   zurueck statt den Worker abstuerzen zu lassen.
2. **Delegation = `"responses"`**, NICHT `"client"`. Client-Delegation haette einen kompletten Umbau
   aller ~20 Tool-Handler erfordert (eigene `llm.LLM`, eigener Call-Loop, kein `@function_tool` mehr
   auf der `Agent`-Instanz) — fuer eine Vorab-Integration auf einem noch offenen, ungetesteten PR zu
   riskant und zu gross fuer den Rahmen dieser Aufgabe. Mit `responses` bleiben
   `voice_worker/main.py`s `@function_tool`-Methoden (create_task, dispatch_to_agent, query_memory, …)
   unveraendert funktionsfaehig — live bewiesen (siehe unten).
3. **Backend-Modell = `jarvis_core.frontier.resolve_model()`** (aktueller Frontier-Default `gpt-5.5`,
   ueberschreibbar per `JARVIS_FRONTIER_MODEL`), NICHT `GPTLiveModel`s eigener Default
   `"gpt-5.6-luna"` — ein Codename-Snapshot ohne dokumentierte allgemeine Verfuegbarkeit (siehe
   `jarvis_core/frontier.py`-Docstring, Stand 10.07.2026). Eine Quelle der Wahrheit fuer "welches
   Modell denkt fuer Jarvis", ob per Voice-Tool (`ask_frontier`) oder als GPT-Live-Backend.
4. **Separates Test-Image + Test-Worker, Produktions-Worker unberuehrt.** Neues
   `voice_worker/Dockerfile.gpt-live` installiert `livekit-agents` + `livekit-plugins-openai` aus dem
   PR-SHA per `pip install --force-reinstall --no-deps git+...` ZUSAETZLICH zu den normalen
   `requirements.txt` (PyPI hat bereits ein `livekit-plugins-openai==1.8.0` OHNE `GPTLiveModel` — ohne
   `--force-reinstall` haelt pip die Versionsnummer fuer "already satisfied" und installiert die
   PR-Variante NIE; live reproduziert). Der Test-Container laeuft unter eigenem `AGENT_NAME` (LiveKit
   `WorkerOptions.agent_name`) — NUR explizites Dispatch (`CreateAgentDispatchRequest`) erreicht ihn;
   automatisches Dispatch (wie es der Produktions-Worker ohne `agent_name` nutzt) sieht ihn nicht. So
   kann der Test-Worker neben dem Produktions-Worker laufen, ohne ihm je einen Anruf wegzuschnappen.

## Alternativen

- **Client-Delegation von Anfang an:** sauberer langfristig (volle Kontrolle ueber den Tool-Loop,
  Streaming-Antworten via `append_commentary`), aber verlangt einen kompletten Umbau der
  Tool-Architektur auf einem PR-Stand, der sich noch aendern kann. Verworfen fuer diese
  Vorab-Integration — Kandidat fuer eine spaetere Migration, sobald #7212 released ist und die
  Delegation-Semantik stabil ist (siehe "Rueckweg/Aufraeumen" unten).
- **Auf das Release von PR #7212 warten:** sauberer, aber Mark will `gpt-live-1` JETZT ausprobieren.
  Verworfen — das separate Test-Image isoliert das Risiko vollstaendig vom Produktions-Pfad.
- **Denselben Produktions-Worker umbauen** (Dockerfile aendern, Container neu bauen): verboten fuer
  diese Aufgabe (Live-Stack darf nicht angefasst werden) und riskant — ein fehlgeschlagener
  Vorab-Plugin-Build haette Jarvis' bestehende Sprachfunktion gekillt.

## Konsequenzen

### Positiv
- Mark kann `gpt-live-1` sofort testen, ohne den produktiven Jarvis-Voice-Pfad zu riskieren.
- Alle bestehenden Tools/Persona/Briefing-Logik funktionieren unveraendert unter `delegation="responses"`
  — live bewiesen: die GPT-Live-Session hat via Responses-Backend das echte MC-Briefing abgerufen und
  korrekt "10 offen" (offene Tasks) gesprochen (identischer Pfad wie bei Realtime).
- Sauberer Rueckweg: `VOICE_API=realtime` (oder gar nicht setzen) auf dem Produktions-Image — betrifft
  nur den echten Worker nicht, weil der das Plugin gar nicht installiert hat.

### Negativ
- **Kein Release-Datum fuer PR #7212** — dieses Setup ist explizit ein Wegwerf-/Vorab-Zustand.
  Aufraeumen (Dockerfile.gpt-live entfernen, auf released `livekit-agents`/`livekit-plugins-openai`
  wechseln) sobald das PR gemerged + released ist.
- **Client-Delegation nicht umgesetzt.** Sollte GPT-Live's "responses"-Modus in der finalen Version
  entfernt/eingeschraenkt werden, muss der Tool-Loop doch noch umgebaut werden.
- **`_build_realtime_model()` ist in `Dockerfile.gpt-live` gebrochen** (live verifiziert): das Plugin-
  Bump auf `livekit-plugins-openai==1.8.0` aendert die `turn_detection`-API von einem rohen `dict` auf
  ein typisiertes Objekt (`AttributeError: 'dict' object has no attribute 'create_response'`) — betrifft
  NUR das GPT-Live-Test-Image (`VOICE_API=realtime`/`VOICE_PROVIDER=xai` sind dort ungetestet/kaputt),
  NICHT das Produktions-Image (dort weiterhin `~=1.5` gepinnt, live laeuft `1.6.7`).
- ⚠️ **Separater, wichtigerer Fund (ausserhalb dieses ADRs' Scope):** `voice_worker/requirements.txt`
  pinnt `livekit-agents[openai,xai]~=1.5` — das erlaubt laut PEP 440 (`~=1.5` ≙ `>=1.5,<2`) JEDEN
  1.x-Release, aktuell bereits `1.8.0`. Ein stinknormaler Rebuild des PRODUKTIONS-Workers (ohne jeden
  GPT-Live-Bezug, z.B. naechster `docker compose build voice-worker` nach Cache-Bust) wuerde denselben
  `turn_detection`-Crash live ausloesen und Jarvis' Sprachfunktion komplett brechen — reproduziert mit
  dem UNVERAENDERTEN `voice_worker/Dockerfile` (Tag `mission-control-voice-worker:prod-code-check`,
  10.09.2026). Dieses ADR/PR aendert `requirements.txt` bewusst NICHT (ausserhalb des Mandats fuer
  diese Vorab-Integration) — **das ist ein eigenes, dringendes Ticket** (z.B. Pin auf
  `livekit-agents[openai,xai]~=1.6.7` oder Fix im Code fuer die neue `turn_detection`-API), das der
  Team-Lead separat einplanen sollte, bevor der Produktions-Worker das naechste Mal neu gebaut wird.

## Nachschliff (10.09.2026, nach Marks Cutover-Entscheid)

Mark hat noch waehrend der ersten Beweisrunde entschieden: GPT-Live ist der **Ersatz** fuer
Jarvis' Voice-Transport, kein Nebenlaeufer. Der Produktions-Worker wurde direkt umgestellt
(kein dauerhafter Test-Container mehr — alle weiteren Beweise laufen seither ueber
kurzlebige `docker run --rm`-Container mit eigenem `agent_name`, die nach dem Test wieder
verschwinden). Aus dem Review dieses Cutovers (Team-Lead + zweiter Agent) kamen vier
Nachschaerfungen, alle in diesem PR:

1. **Instructions-Split** (Fund: die volle Persona inkl. Tool-Trigger-Tabelle ging
   unveraendert als `session.instructions` (Voice-Layer) raus, das Backend-Responses-Modell
   bekam nur 4 generische Saetze — dort werden die Tools aber tatsaechlich aufgerufen).
   Jetzt: `jarvis_core/persona.py::build_live_voice_instructions()` (kurz, nur Sprechstil,
   ~150 Woerter, KEINE Tool-Regeln) fuer die Top-Level-Agent-`instructions`, und
   `build_live_delegation_instructions()` (volle Tool-/Honesty-/Team-Regeln) fuer
   `delegation.responses.instructions`. Beide bleiben im selben Modul, gespeist aus denselben
   Parametern wie `build_instructions()` (ADR-061 bleibt gueltig, kein Fork).
2. **Stimmen-Pruefung**: `marin` (bisheriger Default) ist tatsaechlich eine GUELTIGE
   gpt-live-1-Stimme — `GPTLiveVoices`-Literal + `DEFAULT_VOICE = "marin"` direkt im
   PR-Code (`gpt_live_model.py`), nicht bloss Doku-Vermutung. `_resolve_live_voice()`
   validiert `VOICE_VOICE_ID` gegen die sechs bekannten Namen (aster, beacon, cinder, marin,
   stone, vesper) und faellt bei Unbekanntem laut auf `marin` zurueck statt eine vermutlich
   falsche Stimme durchzureichen.
3. **Compose-Fix + Auto-Erkennung**: `docker-compose.yml` reichte `VOICE_API` NIE an den
   Container durch (nur `VOICE_MODEL`/`VOICE_PROVIDER` standen in der `environment:`-Liste)
   — genau das ist beim Cutover passiert: Container hatte `VOICE_MODEL=gpt-live-1` aber kein
   `VOICE_API`. Jetzt behoben (`VOICE_API: ${VOICE_API:-realtime}` ergaenzt) UND als
   Sicherheitsnetz im Code: `_resolve_voice_api()` erkennt `VOICE_MODEL`-Praefix `gpt-live`
   automatisch, falls `VOICE_API` trotzdem mal fehlt (mit Log), statt still `gpt-live-1` als
   ungueltiges Realtime-Modell an die falsche API zu schicken.
4. **ADR-Nummer**: dieses Dokument hiess zunaechst 082, wurde aber von PR #491 (Runtime-
   Bindung, w-voice-runtime) belegt (auf main gemergt als #476/082). Umbenannt auf **083**.
   Nach Merge von #491 folgt ein Rebase auf `VoiceChoice`/das dortige `api`-Feld aus der
   Runtime-Bindung statt der reinen Env-Variable — Env bleibt dann nur noch Fallback.

## Nachschliff 2 (10.09.2026, nach Marks erstem echten Anruf)

Marks erster Anruf lief nachweislich ueber GPT-Live (Duplex-Session, gpt-live-Events), zeigte
aber zwei echte Probleme: **16s** zwischen letztem User-Item und erster Assistant-Antwort,
und die Antwort klang "AI-like wie vorher" inkl. unerwuenschter Selbstvorstellung ("ich bin
Jarvis"). Ausserdem: "Er berichtet direkt beim Einstieg (10 Tasks offen, Mark…), das ist
nicht natuerlich." Vier weitere Fixes, alle in PR #490:

5. **Dockerfile konsolidiert.** `voice_worker/Dockerfile.gpt-live` existiert nicht mehr —
   der PR-SHA-Install-Block ist jetzt Teil des regulaeren `voice_worker/Dockerfile`
   (Marks Cutover-Entscheid: kein Nebenlaeufer, also auch kein separates Image mehr). Der
   Build-Time-Import-Check (`RUN python -c "from livekit.plugins.openai.realtime import
   GPTLiveModel..."`) bleibt als harter Fail — ein Build ohne das Plugin geht nie durch.
6. **`_build_realtime_model()`-Fix (unabhaengiger Fund, w-voice bestaetigt real/unmocked
   reproduziert):** das durch die PR-SHA-Installation gebumpte `livekit-plugins-openai`
   (1.8.0) verlangt fuer `turn_detection` jetzt ein typisiertes Objekt
   (`openai.types.beta.realtime.session.TurnDetection`) statt eines rohen dicts — sonst
   `AttributeError: 'dict' object has no attribute 'create_response'`. Betraf sowohl OpenAI-
   als auch xAI-Realtime (beide importieren dieselbe Klasse). Behoben mit Import-Fallback
   (typisiert wenn verfuegbar, sonst dict fuer aeltere Plugin-Versionen) — damit funktioniert
   der dokumentierte Rueckweg `VOICE_API=realtime` jetzt auch auf DIESEM (konsolidierten)
   Image, nicht nur auf dem alten `1.6.7`-Backup-Image. Live verifiziert: unmocked
   `RealtimeModel(...)`-Konstruktion fuer OpenAI UND xAI erfolgreich auf dem
   konsolidierten Image, danach ein voller LiveKit-Ende-zu-Ende-Lauf ueber
   `VOICE_API=realtime` (Antwort: "Ja, startklar. Ich bin bereit...", keine Fehler).
7. **Latenz-Tuning:** Backend-Modell fuer die Responses-Delegation ist jetzt
   `_resolve_live_backend_model()` (Env `JARVIS_LIVE_BACKEND_MODEL`, Default
   **`gpt-5.6-luna`** — GPTLiveModel's EIGENER "Fast mode"-Default, nicht mehr
   `jarvis_core.frontier.resolve_model()`/`gpt-5.5`, ein Reasoning-Modell ohne
   Effort-Limit). Zusaetzlich `reasoning={"effort":"low"}`, `text={"verbosity":"low"}`,
   `service_tier="priority"`, `max_output_tokens=400`. `JARVIS_LIVE_BACKEND_MODEL` ist eine
   EIGENE Env-Var, getrennt von `JARVIS_FRONTIER_MODEL` (ask_frontier) — Full-Duplex
   braucht Tempo, `ask_frontier` darf langsam gruendlich sein. Latenz-Log
   (`delegation_latency_s=…`, transport-unabhaengig via
   `session.on("conversation_item_added")`) live gemessen:
   **16s → 3.32s** (GPT-Live) bzw. 5.68s (Realtime-Rollback) im selben Testlauf.
8. **Instructions weiter geschaerft** (`jarvis_core/persona.py`): Voice-Layer bekommt jetzt
   explizit "never introduce yourself", "stop talking immediately on interruption", und
   versteht Schweizerdeutsch-Input explizit (Antwort bleibt Schweizer-Hochdeutsch).
   Backend-Layer bekommt eine neue "CONFIRMATION ECHO"-Regel: vor jeder
   stop/deploy/delete-artigen Aktion wird zuerst in einem Satz zurueckgemeldet, was passiert,
   bevor das Tool aufgerufen wird.
9. **Begruessung neu entworfen** (situatives Oeffnen, Variante b aus drei erwogenen
   Alternativen — siehe PR-Beschreibung fuer alle drei): Gruss + Vokativ, tageszeit-
   unabhaengig aber KEINE Zahlen mehr. Nur bei echtem Anlass (Approval offen ODER ein Task
   mit `status="blocked"` — "failed" Tasks stehen nicht in der Briefing-API) EIN kurzer
   Zusatz-Satz. Verworfen: (a) Briefing nie erwaehnen (Approvals wuerden untergehen), (c)
   Jarvis wartet erst 1-2s auf Mark (wirkt wie eine tote Leitung bei GPT-Live). Live
   verifiziert: "Hi, Mark – was steht an? Alles klar, sag mir einfach, womit ich dir helfen
   soll." — keine Zahl, keine Selbstvorstellung.

## Nachschliff 3 (10.09.2026, Mark: "soll auch lachen, natürlich wirken wie ChatGPT")

10. **Natuerlichkeit/Ausdruck.** Geprueft im PR-Code (`gpt_live_types.py`/`gpt_live_model.py`):
    es gibt KEIN dediziertes Protokoll-/Session-Feld fuer Emotion/Ausdruckskraft/Tempo —
    `AudioOutput`/`AudioConfig` kennen nur `voice` (Name oder Custom-Voice-Objekt). Der einzige
    Hebel ist Prompt-Text, wie bei OpenAIs bisheriger Realtime-API auch. `LIVE_VOICE_INSTRUCTIONS`
    entsprechend erweitert: explizite Anweisung zu Lachen/"haha" bei etwas Lustigem,
    Zwischenlauten ("hm", "ah okay"), kurzem Zoegern, Tonhoehen-/Tempo-Variation je nach Inhalt
    (schneller/leichter bei Small Talk, langsamer/ruhiger bei Zahlen/Warnungen) — "talk like a
    colleague on a call, not like an assistant reading a screen". Live-Testdialog (Scherz-Prompt,
    ephemeral Worker): Antwort enthielt das angewiesene Backchannel-Token woertlich
    ("Hmm.") und eine natuerliche Rueckfrage statt einer Statusreport-Formulierung — Beleg im
    PR. **Einschraenkung:** ob gpt-live-1 das auch HOERBAR umsetzt (Lachen in der Stimme, echte
    Tonhoehen-Variation), laesst sich aus dem Transkript/PCM-Bytes nicht zuverlaessig pruefen —
    das braucht Marks Ohr am naechsten echten Anruf.

## Nachschliff 4 (10.09.2026) — doppelter Begruessungssatz + 6-Stimmen-Vergleich

11. **Begruessung war doppelt.** Team-Lead-Fund: "Hi, Mark – was steht an? Alles klar, sag mir
    einfach, womit ich dir helfen soll." — der zweite Satz war reiner Fuellstoff, den das
    Voice-Modell trotz der festen Begruessungszeile spontan dranhaengte (vermutlich verstaerkt
    durch die neuen Natuerlichkeits-Instructions aus Nachschliff 3, die zu mehr Gespraechigkeit
    ermutigen). `_build_greeting()`s Anweisung verschaerft: "WORTWOERTLICH und NICHTS SONST —
    kein Zusatz-Satz... auch wenn dir spontan noch etwas Freundliches einfaellt". Live
    verifiziert mit 3 unabhaengigen Ephemeral-Laeufen NACH dem Fix: alle drei Begruessungen
    genau EIN Satz ("Hey, servus, Mark, was machst du?" / "Hallo, hey Marc, was liegt an?" /
    "Hey, Mark. Was liegt an?" — der dritte Lauf hatte zusaetzlichen Text, aber das war eine
    ECHTE Reaktion auf real mitgesendetes Test-Audio, das zeitlich mit der Begruessung
    kollidierte, kein Begruessungs-Fuellstoff mehr).
12. **6-Stimmen-Vergleich** (Protokoll-Smoke, derselbe deutsche Scherz-Prompt gegen jede der
    sechs bekannten `GPT_LIVE_KNOWN_VOICES`): alle sechs (aster, beacon, cinder, marin, stone,
    vesper) antworten funktional identisch — akzeptieren dasselbe Protokoll, liefern
    vergleichbare Latenz, beginnen alle denselben Programmierer-Witz sinnvoll. **Ehrlich:**
    Ausdruckskraft/Timbre/Prosodie lassen sich aus Transkript-Text oder rohen PCM-Bytes nicht
    objektiv vergleichen — das ist eine Hoer-Entscheidung, keine Mess-Entscheidung, ohne
    Audio-Analyse-Tooling (das hier nicht gebaut wurde). **Empfehlung:** `marin` als Default
    beibehalten — es ist der Plugin-eigene Default, in allen bisherigen Tests bewaehrt, und der
    bisherige Realtime-Default (Kontinuitaet). Will Mark eine andere Stimme hoeren, ist das ein
    reiner `VOICE_VOICE_ID`-Env-Change, kein Code-Umbau — am besten per echtem Anruf A/B-testen,
    nicht per Transkript-Vergleich.

## Referenzen

- Betroffene Dateien: `voice_worker/main.py` (VOICE_API-Selector, `_build_live_model`,
  `_build_llm_model`, `_resolve_live_backend_model`, `_attach_latency_logging`,
  `_build_greeting`/`_urgent_note`, typisierte `TurnDetection`, `AGENT_NAME`),
  `voice_worker/Dockerfile` (PR-SHA-Install jetzt der reguläre Build-Pfad —
  `Dockerfile.gpt-live` geloescht), `jarvis_core/persona.py`
  (`build_live_voice_instructions`, `build_live_delegation_instructions`),
  `backend/tests/test_voice_worker_gpt_live_transport.py`, `scripts/gpt_live_protocol_smoke.py`,
  `scripts/gpt_live_livekit_smoke.py`
- Externe Quellen:
  - https://developers.openai.com/api/docs/guides/live
  - https://developers.openai.com/api/docs/guides/voice-websockets?api=live
  - https://developers.openai.com/api/docs/guides/live-delegation
  - https://developers.openai.com/api/docs/guides/live-migration
  - https://github.com/livekit/agents/pull/7212 (Head-SHA `de3c5ce66058c6ab437ad41f963cbaeb39046c6d`,
    geprueft 10.09.2026)
  - https://github.com/livekit/agents/pull/6677 (Duplex-Adapter-Core, Grundlage von #7212)
- Verwandte ADRs: ADR-060 (VOICE_PROVIDER openai/xai), ADR-061 (jarvis_core shared package),
  ADR-062 (ask_frontier / Frontier-Modell-Wahl), ADR-038 (Rename Voice-Agent → Jarvis)
