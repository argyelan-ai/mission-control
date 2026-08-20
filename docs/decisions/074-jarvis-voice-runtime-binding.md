# ADR-074 — Jarvis' Sprach-Anbieter ist eine Runtime-Bindung, kein env-Flip

**Status:** Accepted
**Datum:** 2026-08-21
**Scope:** Backend/DB, Backend/Dispatch, Infra/Runtime (voice-worker), Frontend/State
**Ersetzt:** ADR-060 (Provider-Wahl per env)

## Kontext

Jarvis war der einzige Agent in der Flotte ohne Runtime-Bindung:
`agent_runtime="host"`, `harness=NULL`, `runtime_id=NULL`. Welcher Anbieter
tatsächlich sprach, stand ausschliesslich in `VOICE_PROVIDER` in der Env des
`voice-worker`-Containers.

Drei Folgen davon:

1. **Nicht sichtbar.** Die Agentenseite zeigte ein `locked · host`-Abzeichen.
   Wer wissen wollte, welchen Anbieter Jarvis fährt, musste in den Container
   schauen. Am 20.08.2026 stand in Jarvis' `soul_md` in der DB, im
   compose-Kommentar und im Docstring von `routers/voice.py` übereinstimmend
   „xAI Grok" — der Container fuhr seit ADR-060 OpenAI. Drei falsche Quellen,
   weil keine davon die wirksame war.
2. **Nicht umschaltbar ohne Eingriff.** Ein Wechsel hiess: compose editieren,
   Container neu erzeugen. Für eine Entscheidung, die Mark je nach Bedarf
   treffen will („Grok / OpenAI / später lokal"), ist das keine Bedienung.
3. **Nicht nachweisbar.** Der Worker loggte das Modell nie — 976 Logzeilen, null
   Treffer für „gpt-realtime". Ein Wechsel liess sich weder belegen noch
   widerlegen.

ADR-060 hatte den DB-Weg 2026-07-10 ausdrücklich verworfen, mit einer damals
korrekten Begründung: *„Der voice-worker ist ein Singleton-Container ohne
bestehende Config-Sync-Infrastruktur wie cli-bridge-Agents (kein
`agent_runtime_switch`-Äquivalent)."*

Diese Prämisse ist entfallen. `HOST_ADAPTERS` (ADR-064) ist seither auf sechs
Host-Harnesses gewachsen und trägt die Invariante, dass ein neuer Adapter der
einzige nötige Eingriff ist, um einen Harness end-to-end schaltbar zu machen.
Die Infrastruktur, deren Fehlen ADR-060 begründete, ist da.

## Entscheidung

Der Sprach-Anbieter wird eine **Runtime-Bindung am Jarvis-Agenten** — gewählt im
selben Runtime-Picker wie bei jedem anderen Agenten.

Arbeitsteilung:

- **Die Runtime-Zeile ist Speicher und Anker.** Zwei Seed-Zeilen im Muster von
  `grok-cloud`: `voice-openai` und `voice-xai`. Sie sagen WELCHER Anbieter — nie
  ein Schlüssel. Neues Protokoll `"voice"` in `harness_compat`, damit kein
  CLI-Harness sie binden kann und Jarvis keine Chat-Runtime.
- **`JarvisVoiceAdapter` macht Jarvis schaltbar.** Bewusst inert: `reload()`
  startet nichts neu, `build_agent_env()` liefert `{}`, `bootstrap()` verweigert
  mit Hinweis auf compose.
- **Der Wirkmechanismus ist ein Pull pro Anruf.** Der Worker holt die Bindung
  über `GET /api/v1/agent/voice/config` beim Sitzungsstart. LiveKit vergibt pro
  Anruf einen frischen Raum, also liest jeder Anruf neu.
- **Schlüssel bleiben in der Container-Env.** `OPENAI_API_KEY`/`XAI_API_KEY`
  werden von MC weder gespeichert noch ausgeliefert (ADR-056 Finding 5).
- **Env bleibt Notfall-Default**, für den Fall dass das Backend nicht antwortet.

Leitregel im Worker: **Jarvis wird nicht stumm.** Unbekannter Anbieter → env-Default.
Fehlender Schlüssel → der Arm, der einen hat. Nur wenn gar kein Schlüssel
existiert, wird geworfen; da ist nichts mehr zu retten.

## Alternativen

- **Settings-Block nach Muster PR #333 (`app_settings` + KI-Provider-Tab)** →
  Verworfen. `apply_ai_provider_overrides` schreibt per `setattr` auf das
  In-Memory-Singleton `app.config.settings`. Der voice-worker ist ein eigener
  Prozess und sieht dieses Singleton nie: die Oberfläche hätte grün gemeldet,
  während Jarvis weiter mit dem alten Anbieter spricht. Ausserdem hängen die
  #333-Routen an `require_role(ADMIN)`, während der Worker ein Agent-Token
  trägt. Übernommen wurde das *Prinzip* „Felder pro Arm", nicht der Weg.
- **Beim Umschalten den Container neu starten** → Verworfen. Das Backend könnte
  es (der docker-socket-proxy erlaubt Restarts), aber es würde ein laufendes
  Gespräch mitten im Satz abschneiden. Der Pull pro Anruf macht es überflüssig.
- **Push-Kanal in den Worker (Redis-Abo)** → Verworfen. Der voice-worker hat
  keine Redis-Abhängigkeit, und ein Wechsel mitten im Gespräch ist ohnehin nicht
  gewollt.
- **Stimme mit in die DB** → Verworfen für diese Runde. Die `runtimes`-Tabelle
  hat keine passende Spalte und kein generisches JSON-Feld; die Konvention in
  `role_tags` zu pressen wäre eine versteckte zweite Bedeutung. Stimme bleibt
  pro Arm eine env-Variable. Wenn sie in die Oberfläche soll: eigene Spalte.
- **Dritter Arm „lokal" gleich mitbauen** → Verworfen. Für lokale Sprache fehlen
  zwei von drei Bausteinen (Erkennung tot, Ausgabe existiert nicht). Ein Knopf,
  der Jarvis stumm schaltet, wäre schlimmer als kein Knopf. `VOICE_RUNTIME_TYPES`
  ist so gebaut, dass `voice_local` später ein Eintrag plus eine Seed-Zeile ist.

## Konsequenzen

### Positiv
- Anbieterwechsel ist ein Klick, wirksam ab dem nächsten Anruf, ohne Neustart.
- Der aktuelle Zustand ist in der Oberfläche sichtbar statt in einer Env
  vergraben — und im Log belegbar (`provider=… model=… source=…`).
- Jarvis verhält sich wie der Rest der Flotte; kein Sondersystem.
- Getrennte Stimm-Variablen pro Arm beseitigen eine echte Falle: die
  Stimmnamen der beiden Anbieter sind disjunkt.

### Negativ
- Der Wechsel wirkt **erst beim nächsten Anruf**. Ohne den Hinweis im Dialog
  liest sich das wie ein Fehler; der Hinweistext ist deshalb Teil der Änderung.
- Ein Anruf mehr im Sitzungsstart (3 s Deckel, fail-soft).
- Der Harness „jarvis" taucht im Agenten-Wizard auf. `singleton_slug` und die
  klare Ablehnung in `bootstrap()` fangen die Fehlbedienung ab; kosmetisches
  Restrisiko bewusst akzeptiert.
- Start/Stop auf der Runtimes-Seite bleibt für diese Zeilen wirkungslos — wie
  bei `grok-cloud`. In `startup_notes` dokumentiert.

### Grenzen (wichtig gegen Missverständnisse)
Der Schalter stellt **nur den Sprach-Kanal** um. Der Text-Kanal
(`jarvis_core/brain.py`) und `ask_frontier` (`jarvis_core/frontier.py`) sprechen
weiterhin fest mit OpenAI. Wer den Schalter mit „Datenschutz" oder „alles läuft
jetzt über X" begründet, sitzt einem Trugschluss auf.
