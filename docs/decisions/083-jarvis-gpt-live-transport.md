# ADR-083 — Jarvis GPT-Live-Transport (vorab, LiveKit-PR #7212)

**Status:** Accepted. `_API_TRANSPORTS["live"]` ist seit diesem PR implementiert und im
konsolidierten `voice_worker/Dockerfile` gebaut. Seit dem Merge mit ADR-082 (#491, "Jarvis'
Sprachmodell wird Runtime-Bindung") entscheidet NICHT mehr eine `VOICE_API`-Env-Var, welchen
Transport Jarvis spricht, sondern `classify_voice_api()` anhand des gebundenen/konfigurierten
Modellnamens (`gpt-live-*` → live) — siehe Nachschliff 6. **Deploy-Schritt PFLICHT:** Jarvis muss
im MC-Runtime-Picker explizit auf die Seed-Zeile `voice-openai-live` (`gpt-live-1`) gebunden
werden — ohne diese Bindung bleibt Jarvis auf Realtime, auch mit diesem PR gemergt (Review-Fund,
siehe Nachschliff 7). Weiterhin "vorab", weil LiveKit-PR #7212 noch offen ist (siehe Aufräumen
unten).
**Datum:** 2026-09-10
**Scope:** Infra/Runtime (voice_worker) | Backend/Voice

## Rückweg (falls gpt-live-1/das Vorab-Plugin Probleme macht)

1. **Runtime-Bindung zurücksetzen (der eigentliche Schalter seit ADR-082):** Jarvis im
   MC-Runtime-Picker zurück auf `voice-openai` (`gpt-realtime-2.1`) binden. Wirkt ab dem
   nächsten Anruf, kein Container-Neustart nötig (die Bindung wird pro Anruf frisch gezogen,
   `voice_worker/main.py::entrypoint()`).
2. Falls MC selbst nicht erreichbar ist (Bindung kann nicht gelesen werden): `.env` (Symlink
   `~/.mc/secrets/mission-control/.env`) — `VOICE_MODEL` auf `gpt-realtime-2.1` setzen (NICHT
   `gpt-live-1, sonst klassifiziert der Env-Fallback erneut `api="live"`),
   `VOICE_PROVIDER=openai` (unverändert). Es gibt seit Nachschliff 6 keine `VOICE_API`-Var mehr
   zu setzen/entfernen.
3. Bei einem Image-Rollback zusätzlich: `entrypoint()`s Guard (Nachschliff 7, Review Finding 2)
   fängt genau diesen Fall selbstständig ab — läuft ein älteres Image ohne den Vorab-Plugin-Block
   und die Bindung zeigt trotzdem auf `api="live"`, erzwingt der Guard einen Realtime-Fallback,
   unabhängig davon was `VOICE_MODEL`/MC sagen. Trotzdem sauberer: Image zurück auf
   `mission-control-voice-worker:realtime-backup-20260910` (getaggter Snapshot des vorherigen,
   unveränderten `voice_worker/Dockerfile`-Builds, livekit-agents `1.6.7`),
   `docker compose -p mission-control --env-file .env up -d --force-recreate voice-worker` aus
   `.claude/worktrees/deploy-main`.
4. Wirk-Beweis wie beim Rollout: Worker-Log zeigt `voice config: ... api=realtime`, ein Anruf
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
- ✅ **Requirements-Pin behoben (Stand Nachschliff 5, siehe unten):** `voice_worker/requirements.txt`
  pinnt jetzt `livekit-agents[openai,xai]==1.8.0` — exakt was der Produktions-Build heute tatsaechlich
  installiert (`pip freeze` gegen `mission-control-voice-worker-1`). Der urspruengliche separate
  Mini-PR #494 (`~=1.6.7`) wurde NICHT gemergt — er haette mit dem konsolidierten Dockerfile
  (Basis-Install → force-reinstall aus PR-SHA fuer agents+openai, unveraendert fuer xai) eine
  ungetestete Versions-Mischung ergeben. Aufgeloest direkt in diesem PR/ADR (Nachschliff 5).

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

## Nachschliff 5 (10.09.2026) — Test-Isolation, hörbare Stimmproben, Pin-Aufräumen

13. **Test-Skript hat den Prod-Worker doch erreicht.** Team-Lead-Fund: ein Ephemeral-Test-Room
    wurde vom Produktions-Worker bedient (`agent_name=""`, automatisches Dispatch), obwohl
    `scripts/gpt_live_livekit_smoke.py` `CreateAgentDispatchRequest` fuer einen eigenen
    `agent_name` nutzte. Ursache: das FUEGT einen Dispatch-Job hinzu, verhindert aber NICHT, dass
    LiveKits automatisches Dispatch zusaetzlich jeden Worker mit leerem `agent_name` in denselben
    Room schickt. Fix: der Room wird jetzt VORHER explizit mit
    `room.create_room(CreateRoomRequest(agents=[RoomAgentDispatch(...)]))` angelegt — das ersetzt
    automatisches Dispatch fuer den Room komplett. Live verifiziert: ohne registrierten Test-Worker
    kommt jetzt `"agent_never_joined"` (der Prod-Worker joint nicht mehr automatisch); mit
    registriertem Test-Worker joint genau EIN Teilnehmer (vorher immer zwei).
14. **Hörbare Stimmproben statt nur Transkript.** `gpt_live_protocol_smoke.py` kann jetzt optional
    eine WAV-Datei schreiben (zweites CLI-Argument). Alle sechs `GPT_LIVE_KNOWN_VOICES` mit
    demselben Scherz-Prompt aufgenommen, `scratchpad/voices/<name>.wav` (nicht im Repo — lokale
    Datei fuer Mark zum Reinhoeren), alle valide (5,2s, nicht-stille Amplituden 8k-17k von 32k
    ueberprueft). Ausdruckskraft/Emotion bleibt eine Hoer-Entscheidung, siehe Punkt 12.
15. **Requirements-Pin final geloest** (Team-Lead-Fund: #494s `==1.6.7` haette mit dem
    konsolidierten Dockerfile eine ungetestete Versions-Mischung ergeben — agents/openai werden
    per PR-SHA ueberschrieben, xai nicht). `voice_worker/requirements.txt` jetzt
    `livekit-agents[openai,xai]==1.8.0` — exakt das, was `pip freeze` im laufenden
    Produktions-Container zeigt. Frischer, ungecachter Build verifiziert (Dockerfile-eigener
    GPTLiveModel-Importcheck bestanden), beide Realtime-Pfade (OpenAI + xAI) unmocked
    konstruiert. #494 kann geschlossen werden ("in #490 aufgegangen").

## Nachschliff 6 (10.09.2026) — Merge mit ADR-082 (#491), Registry-Umbau

PR #491 (ADR-082, "Jarvis' Sprachmodell aus der Runtime-Bindung") wurde auf `main` gemergt und
führte parallel dieselbe Grundidee ein wie dieser PR — Provider/Modell/Stimme aus der MC-Runtime-
Bindung statt Env, ein `_API_TRANSPORTS`-Registry-Muster (dort mit einem bewussten Platzhalter
für `"live"`, den ADR-082 selbst als "kommt spaeter" beschreibt) und ``jarvis_core/voice_provider.py``
(``VoiceChoice``, ``classify_voice_api``, ``resolve_voice_choice``, ohne ``livekit``-Import — testbar
im normalen Backend-Job). Nach dem Merge (16.):

16. **Alte Env-basierte Selektion entfernt.** ``_resolve_voice_api()``, ``_build_llm_model()`` und
    die eigenstaendige ``VOICE_API``-Env-Var sind komplett verschwunden — ``classify_voice_api()``
    (ADR-082) leitet die api schon aus dem Modellnamen ab (MC-Bindung ODER ``VOICE_MODEL``-Env-
    Fallback, ``gpt-live-*`` → ``live``), das war objektiv redundant mit meiner eigenen
    ``VOICE_API``-Erkennung. ``docker-compose.yml``/``.env.example`` entsprechend bereinigt statt
    eine tote Config-Zeile stehen zu lassen.
17. **``_API_TRANSPORTS["live"]`` eingehaengt** (der von ADR-082 bewusst offengelassene Platz):
    ``_build_live_transport(choice, *, briefing_ctx, frontier_enabled, operator_name)`` — jeder
    Transport-Builder gibt jetzt ``(llm, agent_instructions)`` zurueck statt nur ``llm``, weil die
    Top-Level-Agent-Instructions transport-abhaengig sind (Instructions-Split, Nachschliff 1).
    ``_build_realtime_transport`` ebenso angepasst (gibt weiterhin die volle Persona zurueck).
    Der zentrale Dispatcher heisst jetzt ``_build_transport`` (vorher zwei kollidierende
    Funktionen namens ``_build_realtime_model`` — eine aus diesem PR ohne Argument, eine aus
    ADR-082 mit ``VoiceChoice``-Argument; ein textueller Merge haette beide unbemerkt
    ueberschrieben, siehe Punkt 18).
18. **Stimmen-Validierung umgestellt:** ``_resolve_live_voice()`` (las ``VOICE_VOICE_ID`` direkt
    aus der Env) → ``_validate_live_voice(voice: str)`` (validiert die von ``VoiceChoice.voice``
    bereits aufgeloeste Stimme — MC-Bindung oder ``VOICE_OPENAI_VOICE_ID``-Env-Fallback, ADR-082).
    Gleiche Logik (bekannte 6 Namen, laute Warnung + Fallback auf ``marin`` bei Unbekanntem), nur
    an die neue Aufloesungs-Kette angeschlossen.
19. **`git merge origin/main` statt Rebase** (weniger Konfliktflaechen bei zwei komplett
    umgebauten Versionen derselben Datei — ein Konfliktdurchlauf statt einer pro Commit). Git
    meldete nur 2 Textkonflikte in ``voice_worker/main.py``, aber die umliegenden "automatisch
    gemergten" Abschnitte waren SEMANTISCH gebrochen (zwei ``_build_realtime_model``-Definitionen,
    fehlender ``import os``, ein Aufruf ohne Pflichtargument) — das ganze Modul wurde deshalb
    manuell aus beiden Versionen neu zusammengesetzt statt dem Text-Merge zu vertrauen.
    Test-Dateien (``test_voice_worker_realtime_provider.py``,
    ``test_voice_worker_gpt_live_transport.py``) entsprechend auf die neuen Funktionsnamen
    umgeschrieben; die vorherige "live api hat keinen Transport, wirft absichtlich" (ADR-082) ist
    jetzt "live api hat einen Transport, baut ein echtes GPTLiveModel".
20. **Testlauf-Einschraenkung (Team-Lead-Anweisung, 10.09.2026):** waehrend Mark aktiv testet,
    KEINE Laeufe gegen den lokalen LiveKit-Server (ein frueherer Ephemeral-Test-Room wurde vom
    Produktions-Worker bedient, siehe Nachschliff 5, Punkt 13 — dasselbe Risiko besteht bei jedem
    lokalen LiveKit-Kontakt, unabhaengig vom Isolation-Fix). Dieser Merge/Umbau ist daher NUR mit
    Unit-Tests (pytest im gebauten Image) + Protokoll-Smoke (direkt gegen die OpenAI-API, kein
    LiveKit-Kontakt) verifiziert — kein LiveKit-Ende-zu-Ende-Lauf fuer diesen Punkt.

## Nachschliff 7 (10.09.2026) — Adversariales Review (rev-490), 7 Punkte

Review-Verdikt: MERGEBAR NACH FIX, 🟡 gelb, mit einem 🔴-Vorbehalt (Befund 1). Live verifiziert per
DB-Query + Code-Lesen + zwei pytest-Laeufen (Backend-venv vs. mit livekit-Stub) + drei
Sabotage-Proben. Volle Fundliste in `scratchpad/review-490.md`; hier nur die Fixes:

21. **🔴 Befund 1 — GPT-Live war nach Deploy AUS.** Seit #491 schlaegt die MC-Bindung die Env; die
    Live-DB hatte (und hat ohne diesen Fix weiterhin) KEINE `gpt-live-*`-Runtime-Zeile, Jarvis war
    an `voice-openai`/`gpt-realtime-2.1` gebunden. Genau invertiert zur Absicht: MC gebunden →
    Realtime, MC down → GPT-Live (Env-Fallback). Fix: neue Seed-Zeile `voice-openai-live`
    (`backend/config/runtimes.json`, `provider=openai`, `model_identifier=gpt-live-1`,
    idempotent per Slug ueber `runtime_seeder.py` — wird beim naechsten Backend-Start eingefuegt,
    bindet aber NICHTS automatisch). **Deploy-Schritt PFLICHT** (siehe Status oben): Jarvis manuell
    im Runtime-Picker auf diese Zeile binden, danach Log-Beweis `api=live source=mc` (aus
    `VoiceChoice.as_log()`) beim naechsten Anruf. Das Binden macht der Team-Lead beim Deploy.
22. **🟡 Befund 2 — Rueckweg-Image war eine tote Leitung.** `entrypoint()`s alter Guard
    (`api not in _API_TRANSPORTS`) kann nie mehr feuern, weil `"live"` jetzt registriert ist — der
    Fall, fuer den er gebaut wurde (Image ohne Vorab-Plugin), landete stattdessen in
    `_build_live_transport()`s `RuntimeError` → Session bricht ab. Guard erweitert um
    `choice.api == "live" and not _GPT_LIVE_AVAILABLE` → `report_voice_unsupported` + ein
    ERZWUNGENER Realtime-`VoiceChoice` (fest `openai`/`gpt-realtime-2.1`), NICHT ein blosser
    `resolve_voice_choice(None)`-Aufruf — der haette bei `VOICE_MODEL=gpt-live-1` (Prod-.env)
    denselben Fehler reproduziert. Modul-Docstring korrigiert.
23. **🟡 Befund 3 — 36 von 45 Tests uebersprangen in CI.** `voice_worker/main.py` importiert
    `livekit` auf Modulebene; jeder Test ueber `_import_main()` skippte still im Backend-venv (=
    CI). Sabotage-Probe des Reviewers fing nur 1 von 3 gebrochenen Asserts. Fix: Begruessung,
    `urgent_note`, GPT-Live-Stimmen-Validierung und die Latenz-Arithmetik sind nach
    `jarvis_core/voice_greeting.py` gewandert (kein `livekit`-Import, genau die Trennung, die
    `voice_provider.py` schon vormacht) — neue `backend/tests/test_jarvis_voice_greeting.py` deckt
    sie OHNE Skip-Bedingung im gewoehnlichen Backend-Testjob ab. `voice_worker/main.py` re-exportet
    die alten Namen (`_build_greeting`, `_urgent_note`, `_validate_live_voice`,
    `GPT_LIVE_KNOWN_VOICES`) fuer Rueckwaertskompatibilitaet.
24. **🟡 Befund 4 — xai + gpt-live-Modellname war eine stille Leitung.** `classify_voice_api`
    liefert fuer Provider `xai` immer `"realtime"`; ein an `voice-xai` gebundenes
    `gpt-live-*`-Modell ging unveraendert ans xAI-Plugin und wurde erst beim Connect abgelehnt.
    Kein harter Stop (das Modell koennte theoretisch existieren), aber ein lauter Log-Warn im
    xai-Zweig von `_build_realtime_transport`, wenn `choice.model` mit `gpt-live` beginnt.
25. **🟢 Befund 5 — Begruessung sagte "Abend" auch morgens.** Der Kommentar behauptete
    "tageszeit-abhaengig", war es aber nur kosmetisch (reiner `random.choice` ueber alle
    Varianten). `build_greeting()` nutzt jetzt echt `briefing["current_time_of_day_de"]`
    (`backend/app/routers/vault.py::_time_of_day_de`, Buckets morgens/mittags/nachmittags/
    abends/nachts) — zeitpassende Gruss-Varianten werden nur bei passender Tageszeit in den
    Auswahl-Pool gemischt, "Abend" kann nicht mehr morgens fallen. Test dafuer in
    `test_jarvis_voice_greeting.py` (positiv UND negativ pro Tageszeit).
26. **🟢 Befund 6 — Voice-Block hatte keine eigene Ehrlichkeits-Regel.** Ergaenzt in
    `LIVE_VOICE_INSTRUCTIONS`: "Never state a task/agent fact yourself — only what the backend
    actually delivered. Don't invent a status, a number, or an outcome while waiting; say 'schau
    ich nach' and wait for the real answer instead."
27. **🟢 Befund 9 — Doku nannte die geloeschte `VOICE_API`-Variable.** `README.md:361` (Zeile
    gestrichen, `JARVIS_LIVE_BACKEND_MODEL` bleibt), ADR-Kopf/Rueckweg oben umgeschrieben auf die
    Runtime-Bindung als eigentlichen Schalter, `voice_worker/Dockerfile`-Kommentar korrigiert,
    ADR-083-Eintrag in `docs/ARCHITECTURE.md` nachgetragen. `xai`-Preload-Check
    (`python -c "from livekit.plugins import openai, xai"`) im Dockerfile wieder ergaenzt — war im
    konsolidierten Build ersatzlos weggefallen, der neue Check prueft nur `openai`/`GPTLiveModel`.
    Befund 7 (Dockerfile-Vorbehalte) und Befund 10 (Test ohne Aussagekraft, `AGENT_NAME`) sind
    bewusst dokumentierte Restrisiken bzw. wurden beim Test-Umzug (Befund 3) entfernt statt
    "gefixt" — Befund 8 (Secrets/Privacy) war schon gruen.

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
