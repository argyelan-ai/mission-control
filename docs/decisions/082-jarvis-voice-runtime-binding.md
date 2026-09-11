# ADR-082 — Jarvis' Sprachmodell wird Runtime-Bindung statt Env-Variable

**Status:** Accepted
**Datum:** 2026-09-10
**Scope:** Backend/Runtime · Backend/API · Infra/Runtime (voice-worker) · Backend/Config

## Kontext

Jarvis war der einzige Agent, dessen Sprach-Anbieter/Modell NICHT über die
Runtime-Registry lief (ADR-027/028/048): `voice_worker/main.py::_build_realtime_model()`
las `VOICE_PROVIDER`/`VOICE_MODEL` direkt aus der Container-Env
(`docker-compose.yml`, gesetzt durch `.env`). Ein Modellwechsel
(`gpt-realtime-2.1` → `gpt-live-1`) bedeutete `.env` editieren + `docker
compose up -d voice-worker` — jeder andere Agent switcht sein Modell im
MC-Runtime-Picker ohne Deploy.

Das ist genau das Problem, das ADR-064 (HostHarnessAdapter-Registry) für
host-launchd-Agenten (Hermes, Grok, Kimi, Boss) bereits gelöst hat: eine
Registry entscheidet zentral, welche Host-Harnesses eine Runtime-Bindung
tragen dürfen und wie ein Switch in-place wirkt, ohne die generische
cli-bridge-Container-Maschinerie (Compose-Rewrite, Restart) anzufassen.
Jarvis ist kein CLI-Harness — er läuft als eigener docker-compose-Service
(Profil `voice`, LiveKit-Room-Join, Realtime-Speech-Socket, kein tmux, kein
`agent.env`) — passt aber strukturell in dieselbe Registry: „darf MC diesen
Agenten switchen, und wie wirkt der Switch" ist exakt dieselbe Frage.

Ein früherer Anlauf (PR #339, ADR-074, August 2026) hatte dasselbe Ziel und
denselben Kern-Entwurf, wurde aber nie gemerged und ist seither 194 Commits
hinter `main` zurückgefallen (Slot-Runtimes ADR-078, `supports_vision`,
`serving_since`, mehrere Router-Refactors). Dieses ADR ersetzt ADR-074 mit
demselben Entwurf, portiert auf den aktuellen Stand.

## Entscheidung

**Jarvis bekommt eine echte Runtime-Bindung** (`agents.runtime_id`), genau
wie jeder andere Agent. Zwei Seed-Anker-Zeilen (bereits in der Live-DB:
`voice-openai` / `voice_openai` / `gpt-realtime-2.1`, `voice-xai` /
`voice_xai` / `grok-voice-think-fast-1.0`) sagen WELCHER Anbieter — nie ein
Schlüssel.

1. **Neues Wire-Protokoll `"voice"`** in `harness_compat.py`
   (`VOICE_RUNTIME_TYPES = {"voice_openai": "openai", "voice_xai": "xai"}`,
   klassifiziert in `runtime_protocol()` VOR dem `_OPENAI_TYPES`-Check — sonst
   würde `voice_openai` als `"openai"` durchrutschen und jeder
   openai-sprechende CLI-Harness (openclaude/omp/hermes) erschiene
   kompatibel). `HARNESS_PROTOCOLS["jarvis"] = {"voice"}`.

2. **`JarvisVoiceAdapter`** in `HOST_ADAPTERS` (`host_harness_adapter.py`) —
   bewusst inert: `build_agent_env` liefert `{}` (kein `agent.env` existiert
   für Jarvis), `bootstrap()` weist mit 422 ab (MC provisioniert nicht,
   docker-compose tut das), `reload()` ist ein No-op mit Log-Zeile (Neustart
   würde einen laufenden Anruf kappen — unnötig, weil der Worker pro Anruf neu
   pullt). `sync_host_agent_model()` bricht für Protokoll `"voice"` früh ab
   (bereits vorhandener Codepfad, keine Änderung nötig) — ein Switch schreibt
   also nie eine Datei.

3. **`GET /api/v1/agent/voice/config`** (`agent_scoped.py`, nur Harness
   `jarvis`, sonst 403) — liefert `{provider, model, voice_id, runtime_slug,
   updated_at}`, nie Schlüsselmaterial. `services/voice_runtime.py::
   resolve_voice_config()` löst die Bindung auf, fail-soft: keine/ungültige
   Bindung → `{provider:"openai", model:null, ...}` + Warnung im Log, nie ein
   Fehler — der Worker darf nie verstummen.

4. **Wirkmechanismus: Pull pro Anruf.** `voice_worker/main.py::entrypoint()`
   ruft `jarvis_core.mc_client.voice_config()` (3s Timeout, fail-soft `None`)
   VOR dem Aufbau des Realtime-Modells. LiveKit gibt pro Anruf einen frischen
   Room — ein Wechsel im Runtime-Picker wirkt also ohne Container-Neustart ab
   dem nächsten Anruf.

5. **Entscheidungslogik getrennt vom livekit-Import.** `jarvis_core/
   voice_provider.py::resolve_voice_choice()` trägt die ganze Regel (MC
   schlägt Env, Env schlägt Hardcoded-Default, fehlender Key auf dem
   gewählten Arm schaltet auf den anderen um, kein Key auf beiden Armen
   raist) OHNE ein einziges `livekit`-Import — das lief 2026-08-21 bereits
   einmal schief: zehn Worker-Tests lebten in einem Modul, das `livekit`
   brauchte, fehlte das Paket in der Backend-venv wurden sie still
   übersprungen und als grün gemeldet. `voice_worker/main.py::
   _build_realtime_model()` bleibt der einzige Ort mit `livekit`-Import und
   ist entsprechend dünn (nimmt eine fertige `VoiceChoice`, baut nur noch das
   Plugin-Objekt).

6. **Modellwechsel-Weg für den Operator:** die bereits existierende
   `PATCH /api/v1/runtimes/db/{slug}` (generisch, nicht auf probe-bare
   Runtime-Typen beschränkt — sie ist bereits der einzige Weg für
   nicht-probebare Cloud-Runtimes wie Anthropic) reicht unverändert: sie
   emittiert `runtime.model_changed` + ruft `mark_agents_for_sync()`, das für
   Host-Agenten mit Adapter bereits `sync_host_agent_model()` +
   `adapter.reload()` aufruft und `agent.model` nachzieht — **kein neuer
   Code nötig**, der bestehende Propagationspfad (ADR-054, erweitert
   ADR-064/078) trägt Jarvis einfach mit, sobald der Adapter registriert ist.

7. **Stimme pro Arm** (`VOICE_OPENAI_VOICE_ID`/`VOICE_XAI_VOICE_ID`, Backend-
   UND voice-worker-Env) — die Stimmnamen sind disjunkt zwischen Anbietern
   ("marin" bei OpenAI, "ara" bei xAI), eine geteilte Variable zerlegte den
   jeweils inaktiven Arm.

8. **Kein Migrations-Bedarf.** `agents.harness = 'jarvis'` steht bereits in
   der Live-DB (aus einem früheren, unabhängigen Schritt) — das Feld existiert
   und ist gesetzt, nur die Compat-Matrix + der Adapter fehlten.

9. **Seed-Zeilen für Fresh-Installs.** Die beiden Anker-Zeilen lebten bisher
   nur in dieser (Mark's) Live-DB, nicht im `config/runtimes.json`-Seed, den
   `runtime_seeder.py` bei jedem Erststart einliest (OSS-Self-Host-Pfad,
   idempotent pro Slug). Ohne Seed-Zeile hätte ein frischer Self-Host-Deploy
   Jarvis ohne jede bindbare Runtime dagestellt. Beide Typen zusätzlich in
   `runtime_naming.CURATED_RUNTIME_TYPES` — sonst leitet die Namens-Regel aus
   `model_identifier` + bekanntem Provider-Host (`api.openai.com`/`api.x.ai`)
   einen chat-modell-artigen Namen ab und überschreibt den kuratierten
   "Jarvis Voice — …"-Titel beim ersten Start.

10. **`api`-Feld: Realtime vs. Live (Follow-up, gleicher Tag).** OpenAI's
    Live API (`v1/live/sessions`, WebSocket/WebRTC/SIP, Voice-Modell entkoppelt
    vom Backend-Modell via Client-/Responses-Delegation) ist ein ANDERES
    Wire-Protokoll als die Realtime API, die dieses Workers livekit-Plugin
    spricht — gleicher Anbieter, disjunktes Format (live an OpenAIs Doku
    geprüft, 2026-09-10). `gpt-live-*`-Modellnamen sind das einzige Signal,
    welche der beiden APIs eine gebundene Zeile meint. Die Antwort trägt
    deshalb zusätzlich `api: "realtime" | "live"`
    (`jarvis_core.voice_provider.classify_voice_api`, EINE Regel, backend-
    und worker-seitig geteilt — der Worker klassifiziert das FINAL gewählte
    (Provider, Modell) unabhängig neu, weil ein Key-Fallback den Arm nach
    MCs Antwort noch wechseln kann).

    **Kein Live-Transport in diesem PR** — nur die Erkennung + eine saubere
    Ablehnung: `voice_worker/main.py::entrypoint()` prüft `voice_choice.api`
    gegen `_API_TRANSPORTS` (heute nur `{"realtime": ...}`) BEVOR das Modell
    gebaut wird; fehlt ein Builder, loggt der Worker laut, meldet es via
    neuer Route `POST /api/v1/agent/voice/unsupported-model` (Activity-Event
    `agent.voice_unsupported_model`, damit die Drift im Feed sichtbar wird
    statt erst beim nächsten Anruf aufzufallen) und fällt auf die reinen
    Env-Defaults zurück (Jarvis bleibt sprechfähig — nie ein stiller
    Fehlschlag mit falschem Endpoint). `_build_realtime_model()` selbst
    dispatcht über dieselbe `_API_TRANSPORTS`-Registry und raist nur noch
    defensiv (letzte Verteidigungslinie, kein normaler Pfad). Ein Live-
    Transport ist später EIN Builder + EIN Registry-Eintrag, keine
    Restrukturierung.

## Alternativen

- **LiteLLM-Proxy als Schnittstelle (wie ADR-064 für Hermes verworfen):**
  hätte einen dritten Netzwerk-Hop pro Sprach-Turn eingeführt — bei Realtime-
  Speech (niedrige Latenz kritisch) inakzeptabel. Verworfen.
- **Runtime-Bindung nur als Anzeige, Switch weiter über Env:** hätte die
  Picker-UI gelogen (zeigt einen Wert, den ein Switch nicht ändert) — exakt
  das ADR-056-Finding-5-Muster (Anzeige ≠ Wirkung). Verworfen.
- **Polling statt Pull-pro-Anruf:** ein Hintergrund-Job, der den Worker bei
  Bindungsänderung neu startet, hätte einen laufenden Anruf gekappt und eine
  weitere bewegliche Komponente (Watcher, Redis-Pub/Sub) für einen Fall
  eingeführt, den "beim nächsten Anruf neu lesen" bereits kostenlos löst.
  Verworfen.

## Konsequenzen

### Positiv
- Jarvis' Sprachmodell ist im selben Picker umschaltbar wie jeder andere
  Agent — kein `.env`-Edit, kein Deploy für einen Modellwechsel.
- Der bestehende Propagationspfad (`mark_agents_for_sync`/`_sync_one`) trägt
  Jarvis ohne neuen Code — ein Beleg dafür, dass die Adapter-Abstraktion aus
  ADR-064 wie vorgesehen trägt.
- Die Entscheidungslogik ist ohne `livekit` testbar (gewöhnlicher Backend-
  Testlauf), die livekit-abhängigen Tests laufen gezielt gegen das echte
  voice-worker-Image statt still zu skippen.

### Negativ
- Ein zweiter API-Call pro Anruf (Backend-Roundtrip vor Session-Start) — bei
  3s-Timeout und Fail-Soft-Fallback ein vertretbarer Overhead gegenüber dem
  bisherigen sofortigen Container-Start.
- Die Grenze muss in jeder Erklärung mitgesagt werden: der Schalter stellt
  NUR den Sprach-Kanal um. Text-Kanal (`jarvis_core/brain.py`, Telegram) und
  `ask_frontier` bleiben fest auf ihrer eigenen Konfiguration.
- Ein `gpt-live-*`-Bind läuft NICHT — der Picker lässt es zu (die Runtime-Zeile
  ist gültig, `is_compatible()` prüft nur Provider/Harness, nicht die
  API-Sub-Klassifikation), aber der Worker refused loud statt zu sprechen,
  bis ein Live-Transport nachgerüstet ist (offener Folge-PR).

## Referenzen

- Betroffene Dateien: `backend/app/services/harness_compat.py`,
  `backend/app/services/voice_runtime.py`,
  `backend/app/services/host_harness_adapter.py`,
  `backend/app/services/agent_runtime_switch.py`,
  `backend/app/services/runtime_naming.py` (`CURATED_RUNTIME_TYPES`),
  `backend/app/routers/agent_scoped.py`, `backend/config/runtimes.json`,
  `jarvis_core/voice_provider.py`, `jarvis_core/mc_client.py`,
  `voice_worker/main.py`, `docker-compose.yml`, `.env.example`
- Verwandte ADRs: ADR-027, ADR-028, ADR-038 (Rename Voice-Agent → Jarvis),
  ADR-048, ADR-054 (Runtime→Agent-Modell-Propagation), ADR-056 (Finding 5,
  keine Schlüssel im Response), ADR-060 (env-basierter Provider-Switch — vom
  Runtime-Picker abgelöst), ADR-061 (`jarvis_core` geteiltes Package),
  ADR-062 (Jarvis Intelligence/`ask_frontier`), ADR-064 (HostHarnessAdapter-
  Registry, Muster für diesen Adapter), ADR-074 (früherer, nie gemergter
  Entwurf desselben Ziels — ersetzt durch dieses ADR)
