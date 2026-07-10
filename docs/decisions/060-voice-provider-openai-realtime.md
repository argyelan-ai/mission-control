# ADR-060 — Voice-Provider-Switch zu OpenAI Realtime, env-basierter xAI-Fallback

**Status:** Accepted
**Datum:** 2026-07-10
**Scope:** Infra/Runtime (voice-worker), Backend/Config (env), Docs

## Kontext

Der `voice-worker`-Container (Jarvis-Persona, ADR-038) sprach bisher
ausschliesslich über xAI's Grok Realtime API (`livekit-plugins-xai`),
fest im Code verdrahtet in `voice_worker/main.py::VoiceAssistant.__init__`.
xAI's Realtime-API ist laut Kommentar im Code "OpenAI-Realtime-kompatibel"
(gleiche `turn_detection`-dict-Struktur) — das legt nahe, dass ein
Provider-Wechsel technisch günstig ist, sollte xAI mal ausfallen oder
OpenAI's Modell (`gpt-realtime-2.1`, GA seit 2026-07-06) qualitativ
vorne liegen.

Ein hart verdrahteter Single-Provider hat zwei Probleme:
- Kein Fallback bei Provider-Ausfall — der Operator verliert Jarvis
  komplett bis zum nächsten Deploy.
- Kein A/B-Vergleich möglich ohne Code-Änderung + Rebuild.

## Entscheidung

**`VOICE_PROVIDER` env var** (Default: `openai`) steuert, welches
Realtime-Modell `voice_worker/main.py::_build_realtime_model()` baut:

- `openai` (Default) — `livekit.plugins.openai.realtime.RealtimeModel`,
  Modell `VOICE_MODEL` (Default `gpt-realtime-2.1`), Voice `marin`
  (überschreibbar via `VOICE_VOICE_ID`). Braucht `OPENAI_API_KEY`.
- `xai` (Fallback) — bisheriges Verhalten unverändert:
  `livekit.plugins.xai.realtime.RealtimeModel`, Voice `ara`
  (überschreibbar via `VOICE_VOICE_ID`). Braucht `XAI_API_KEY`.

`turn_detection` bleibt provider-übergreifend identisch (`_TURN_DETECTION`
Modul-Konstante) — beide Plugins akzeptieren dieselbe
`server_vad`-dict-Struktur.

Fail-fast: fehlt der API-Key des gewählten Providers, wirft
`_build_realtime_model()` sofort einen `RuntimeError` mit klarer
Anleitung (welche env var setzen, oder auf den anderen Provider
umschalten) — statt erst beim ersten LiveKit-Session-Connect
kryptisch zu scheitern.

`requirements.txt` installiert jetzt beide Extras
(`livekit-agents[openai,xai]~=1.5`), damit der Fallback ohne Rebuild
per env-var-Wechsel + Container-Restart funktioniert.

## Alternativen

- **Nur OpenAI, xAI komplett entfernen** → Verworfen. Kein Fallback bei
  OpenAI-Ausfall, und xAI-Integration (Grok Realtime) war bereits
  produktiv erprobt — Wegwerfen ohne Not ist unnötiges Risiko für eine
  reine Provider-Präferenzentscheidung.
- **Provider zur Laufzeit per API umschaltbar (DB-Feld statt env)** →
  Verworfen für v0. Der voice-worker ist ein Singleton-Container ohne
  bestehende Config-Sync-Infrastruktur wie cli-bridge-Agents (kein
  `agent_runtime_switch`-Äquivalent). Env-var + Restart ist der
  bestehende Pattern für diesen Service (`VOICE_VOICE_ID` etc.) — DB-Feld
  wäre Overengineering für einen Service ohne Multi-Instance-Bedarf.
- **`model=` fest auf `"gpt-realtime"` (Plugin-Default) statt
  konfigurierbar** → Verworfen. `VOICE_MODEL` env var erlaubt Wechsel auf
  neuere Realtime-Modelle ohne Code-Änderung, analog zu `VOICE_VOICE_ID`.

## Konsequenzen

### Positiv
- Provider-Wechsel ist ein env-var-Flip + Container-Restart, kein
  Code-Deploy.
- xAI bleibt als getesteter Fallback erhalten — kein Totalausfall bei
  OpenAI-Störung, sofern beide Keys im `.env` gepflegt sind.
- Fail-fast-Fehlermeldung spart Debugging-Zeit beim ersten Setup
  (bisher: kryptischer Fehler tief im LiveKit-Connect-Stack).
- `_build_realtime_model()` ist isoliert unit-testbar (Plugin-Konstruktoren
  gemockt) — keine echten API-Calls nötig für Coverage der
  Provider-/Voice-/Fail-fast-Logik.

### Negativ
- Zwei API-Keys im `.env` zu pflegen statt einem, falls der Operator
  echten Fallback-Betrieb will (sonst reicht der Default-Provider-Key).
- Docker-Image wird etwas grösser (`livekit-agents[openai,xai]` statt
  nur `[xai]`).
- `VOICE_VOICE_ID` hat jetzt provider-abhängige Defaults (`marin` vs.
  `ara`) statt eines einzigen globalen Defaults — Doku-Pflicht in
  `.env.example`, sonst verwirrend beim Providerwechsel.

## Referenzen

- Code: `voice_worker/main.py::_build_realtime_model()`,
  `voice_worker/main.py::_TURN_DETECTION`
- Tests: `backend/tests/test_voice_worker_realtime_provider.py`
- Env: `.env.example` (Voice-agent-Sektion), `docker-compose.yml`
  (voice-worker service: `OPENAI_API_KEY`, `VOICE_PROVIDER`, `VOICE_MODEL`)
- Verwandte ADRs: ADR-038 (Rename Voice-Agent → Jarvis, Persona/Infra-Boundary)
