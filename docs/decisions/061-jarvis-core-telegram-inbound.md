# ADR-061 — jarvis_core (geteilte Persona/Tools/Brain) + Telegram-Inbound

**Status:** Accepted
**Datum:** 2026-07-10
**Scope:** Backend/Services · Backend/Config · Infra/Compose · voice_worker · Docs

## Kontext

Jarvis (ADR-038) lebte bisher ausschliesslich im `voice_worker`-Container: die
Persona (`JARVIS_INSTRUCTIONS`), 12 `@function_tool`-Methoden und der
HTTP-Tool-Client (`voice_worker/mc_client.py`) waren alle fest an LiveKit +
das Realtime-Modell gebunden. Der Operator erreicht Jarvis also nur am
Schreibtisch mit Mikrofon.

Ziel (Welle V1): Jarvis **mobil via Telegram** — Text- und Sprachnachrichten
rein, Text-Antwort raus. Dafür braucht es dieselbe Persona und dieselben Tools
über einen zweiten Kanal, ohne die Logik zu duplizieren. Ein Text-Kanal hat
zudem kein Realtime-Modell — er braucht einen klassischen Chat-Completions-Loop
mit Function-Calling.

## Entscheidung

### 1. Geteiltes Package `jarvis_core/` (Repo-Root)

Persona, Tools, MC-Client und ein Text-Gehirn wandern in ein kanal-agnostisches
Package:

- **`persona.py`** — `PERSONA_CORE` (kanal-agnostisch: wer Jarvis ist, Team-Roster,
  Regeln, Tool-Trigger, Concierge-Mode) + `VOICE_ADDENDUM` (Aussprache,
  gesprochene Brueckenwoerter, Voice-Drawer-Cards) + `TELEGRAM_ADDENDUM` (kein
  Display, Links/Pfade im Text, Graph nur am Desk). `build_instructions(channel,
  briefing_ctx)` setzt Core + Addendum zusammen.
- **`channels.py`** — `Channel`-Capabilities (`supports_cards`,
  `supports_graph_highlight`); `VOICE` und `TELEGRAM`.
- **`tools.py`** — provider-neutrale `ToolSpec`s (Name, Beschreibung, JSON-Schema,
  async Handler, verfügbare Kanäle). Handler bekommen `(client, channel, **kwargs)`
  und **degradieren pro Kanal**: `show_*` pusht am Desk eine Card, auf Telegram
  liefert es Text/Link; `highlight_graph` ist voice-only (Telegram → höfliche
  Ablehnung `desk_only`). `openai_tool_schemas(channel)` + `dispatch(...)`.
- **`mc_client.py`** — der bisherige `voice_worker/mc_client.py`, per `git mv`
  hierher verschoben (Historie erhalten). Base-URL/Token aus Env pro Prozess.
- **`brain.py`** — `JarvisBrain`: Text-Modus. Nimmt User-Text + History, ruft die
  OpenAI Chat-Completions-API mit Function-Calling, führt Tool-Calls über
  `tools.dispatch` aus, liefert finalen Text + ausgeführte Aktionen. Bewusst
  über `httpx` statt des `openai`-SDK — das Backend hat `httpx` schon, keine neue
  schwergewichtige Dependency + Lock-Regeneration. `transcribe_audio()` nutzt die
  OpenAI-Transcription-API (ogg/opus direkt, kein ffmpeg).

`voice_worker/main.py` wird ein **dünner Wrapper**: importiert Persona + Tools +
mc_client aus `jarvis_core`, die `@function_tool`-Methoden delegieren an die
geteilten Handler (`channel=VOICE`). Verhalten identisch — alle bestehenden
Voice-Tests bleiben grün.

### 2. Telegram-Inbound im Backend

- `telegram_bot.py`: `allowed_updates` um `"message"` erweitert; Polling wird
  **nur** gestartet, wenn das Feature gegatet ist (sonst unverändert: keine
  getUpdates-Schleife, nur Approval-URL-Buttons). Neuer `get_file_bytes()`-Helfer
  (getFile → Download) für Sprachnotizen.
- `services/jarvis_telegram.py` (`JarvisTelegramHandler`):
  - **Hartes chat_id-Gate:** nur Nachrichten aus `settings.telegram_chat_id`
    werden verarbeitet; alles andere wird geloggt und ignoriert (kein Reply an
    Fremde).
  - Text → direkt an `JarvisBrain`. Voice (`message.voice`, ogg/opus) → Download →
    Transcribe → Brain; Transkript wird der Antwort vorangestellt („🎤 Verstanden:
    …“), damit der Operator STT-Fehler sofort sieht.
  - Konversations-History pro Chat in Redis (letzte 20 Messages, TTL 24h).
  - Tool-Calls laufen über den geteilten `mc_client` gegen den agent-scoped
    API-Pfad mit dem Jarvis-Token — **kein Auth-Bypass, keine Direkt-DB**.

**Feature-Gate:** alles aktiv nur bei `JARVIS_TELEGRAM_ENABLED=true` UND
`OPENAI_API_KEY` UND `JARVIS_AGENT_TOKEN` gesetzt. Sonst Verhalten exakt wie
heute.

### 3. Build-Kontext / Deploy

- **voice_worker:** Build-Kontext auf Repo-Root gehoben
  (`context: .`, `dockerfile: voice_worker/Dockerfile`), damit das
  Dockerfile `jarvis_core/` mit ins Image kopieren kann. Der Service wird nur
  lokal gebaut (nicht in der GHCR-Release-Matrix), das Anheben ist also risikolos.
- **backend:** Build-Kontext bleibt `./backend` (auch der GHCR-Image-Pfad).
  `jarvis_core` liegt im Repo-Root, ausserhalb dieses Kontexts — es wird daher als
  **Live-Mount** (`./jarvis_core:/app/jarvis_core:ro`) bereitgestellt, exakt wie
  `./backend/templates` und `./docker` heute schon live gemountet sind. Der
  Backend-Import von `jarvis_core` ist **lazy + feature-gated**: fehlt der Mount
  (reines GHCR-Image ohne Repo-Checkout), bleibt das Inbound-Feature still aus.

## Alternativen

- **jarvis_core baken in das Backend-Image (context ./backend belassen, Package
  unter `backend/jarvis_core`)** → Verworfen zugunsten der Repo-Root-Lage, die die
  Symmetrie „ein geteiltes Package, beide Kanäle importieren `jarvis_core`" wahrt
  und dem bestehenden Live-Mount-Muster (templates/docker) folgt.
- **Backend-Build-Kontext auf Repo-Root heben (wie voice_worker)** → Verworfen.
  Das Backend-Image wird per GHCR-Release-Matrix mit `context: ./backend` gebaut;
  ein Kontext-Wechsel würde das `COPY . .` auf den gesamten Repo-Root (inkl.
  `frontend-v2`, `.git`) ausdehnen und die Image-Semantik + CI ändern — zu
  riskant für ein optionales Feature.
- **`openai`-SDK statt httpx** → Verworfen. Neue schwergewichtige Dependency +
  `requirements.lock`-Regeneration für einen einzelnen Chat-Completions-Loop, den
  `httpx` (schon vorhanden) genauso bedient.
- **ffmpeg-Konvertierung der Sprachnotizen** → Nicht nötig. Die
  OpenAI-Transcription-API akzeptiert ogg/opus direkt; das Backend-Image bleibt
  schlank.

## Konsequenzen

### Positiv
- Eine Quelle der Wahrheit für Persona + Tools über alle Kanäle; ein neuer Kanal
  ist künftig „Wrapper + Channel-Definition".
- Jarvis wird mobil (Telegram Text + Voice) ohne die Voice-Erfahrung zu berühren.
- Kein neuer Auth-Pfad: Telegram-Tool-Calls nutzen denselben agent-scoped Token
  wie Voice (Scopes greifen).
- Default-off + lazy Import: Bestandsverhalten (Approval-URL-Buttons) unverändert,
  reine GHCR-Images ohne Mount sind unbeeinflusst.

### Negativ
- Der `jarvis_core`-Live-Mount ist ein impliziter Deploy-Kontrakt für das
  Backend-Feature — dokumentiert in `.env.example`/compose, aber ein reines Image
  ohne Repo-Checkout kann das Feature nicht aktivieren.
- Zwei Build-Kontexte für ein Package (voice: baked, backend: mounted) — eine
  bewusste Asymmetrie, die die GHCR-Backend-CI intakt hält.

## Nicht-Ziele (V1)

Kein TTS-Audio-Reply, keine Voice-Approvals, kein MCP, kein UI — folgen in V2.
