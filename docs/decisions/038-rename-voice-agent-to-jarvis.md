# ADR-038 — Rename Voice-Agent zu Jarvis

**Status:** Accepted
**Datum:** 2026-05-16
**Scope:** Backend/DB (Agent-Row + Activity-Audit), voice-worker Persona,
Env-Vars, Test-Fixtures, Docs

## Kontext

Der xAI-Grok-basierte Concierge-Agent (host-runtime, läuft im
`voice-worker` Docker-Service) wurde 2026-05-15 unter dem Namen "Voice"
in die `agents`-Tabelle eingetragen. "Voice" ist gleichzeitig der Name
der LiveKit-Infrastruktur die darunter liegt: `voice-worker` Container,
`/voice/token` + `/voice/graph-highlight` HTTP-Routes, `voice:graph-
highlight` Redis-Channel, `VoiceProvider`/`VoiceWidget`/`VoiceOverlay`
Frontend-Komponenten, LiveKit-Rooms `voice-{user_id}-…`.

Diese Doppel-Belegung war seit Tag 1 lästig:

- Jeder grep nach "Voice" liefert eine Mischung aus Persona-, Agent-Row-,
  Infra-Treffern; jeder Code-Reviewer muss pro Hit triagieren ob das den
  Agent meint, das System-Persona-Prompt, oder das LiveKit-Layer.
- Der Operator fragt im Chat "wer hat den Task erstellt?" und sieht "Voice" —
  unklar ob das die Persona war oder ein generischer LiveKit-Service.
- Die Activity-Events-Logs ("Agent Voice created task: …") überlappen
  visuell mit Backend-Logs von `/voice/*` Endpoints.
- Beim heutigen Telegram-Vorfall (ADR-037 Auslöser-Session) postete der
  Researcher eine Telegram-Nachricht mit literal "TELEGRAM"-Prefix, weil
  die SOUL.md nicht klar genug zwischen Channel-Name und Header-Konvention
  unterschied — selbe Klasse von Namensraum-Kollision wie Voice vs voice.

Die "richtige" Lösung wäre die Persona klar zu trennen vom Transport.
"Jarvis" ist der Persona-Name des Operators (siehe dessen "Jarvis-Vision" in
`~/Workspace/CLAUDE.md` Ziele-Sektion). Die LiveKit-Infrastruktur ist
generisch — voice in, voice out — und behält den Namen.

## Entscheidung

**Drei klar getrennte Schichten, eine umbenannt:**

1. **DB-Agent + Persona-Identität** → "Voice" wird zu **"Jarvis"**
   - `agents.name = 'Jarvis'` (UUID `156b915b-2642-4924-a16a-3d91123f9b6c`
     unverändert, PBKDF2-Hash in `agents.agent_token_hash` unverändert)
   - System-Prompt in `voice_worker/main.py:JARVIS_INSTRUCTIONS` ("Du
     bist Jarvis — persoenlicher Concierge des Operators…")
   - Test-Fixtures + Kommentare die den Agent benennen
2. **LiveKit-Infrastruktur** → bleibt **"voice"**
   - Service-Name `voice-worker` im docker-compose
   - HTTP-Routes `/api/v1/voice/token`, `/api/v1/voice/graph-highlight`
   - Redis-Channel `voice:graph-highlight`
   - LiveKit-Room-Naming `voice-{user_id}-{ts}-{rand}`
   - Frontend `VoiceProvider`, `VoiceWidget`, `VoiceOverlay`,
     `useVoiceHighlight`, `VoiceHighlightBridge`
3. **Env-Var-Konvention** → folgt der Persona, nicht der Infra
   - `VOICE_AGENT_TOKEN` wird zu **`JARVIS_AGENT_TOKEN`**
   - `voice_worker/mc_client.py` liest mit Fallback:
     `os.environ.get("JARVIS_AGENT_TOKEN") or os.environ.get("VOICE_AGENT_TOKEN")`
     damit ein nicht-nachgezogenes `.env` keinen 401-Loop verursacht
     (Bootstrap-Schutz für mind. einen Release-Zyklus)

Migration `0120_rename_voice_agent_to_jarvis.py` macht atomar:

```sql
UPDATE agents
SET name = 'Jarvis'
WHERE id = '156b915b-2642-4924-a16a-3d91123f9b6c'
  AND name = 'Voice';

UPDATE activity_events
SET title = replace(title, 'Voice', 'Jarvis')
WHERE agent_id = '156b915b-…' AND title LIKE '%Voice%';
```

Beide UPDATEs sind idempotent (`name='Voice'` / `LIKE '%Voice%'` Guards)
und auf die ID gescoped, sodass eine künftige zweite "Voice"-Row nicht
versehentlich mit-gerenamed wird. Die 5 historischen `activity_events`
werden auf expliziten Wunsch des Operators rewrittten — für ein laufendes
System ohne externe Audit-Konsumenten ist Operator-Klarheit hier wichtiger
als Audit-Immutabilität.

## Alternativen

- **Beide Schichten umbenennen** (`voice-worker` → `jarvis-worker`,
  `/voice/*` → `/jarvis/*`, `VoiceProvider` → `JarvisProvider`) →
  Verworfen, weil die LiveKit-/xAI-Realtime-/WebRTC-Schicht generisch
  voice-in/voice-out ist. Wenn morgen ein zweiter LLM-Backend dazukommt
  (z.B. ein Spanish-only Realtime-Worker), würde "jarvis" als Infra-Name
  ihn ausschliessen. Persona ↔ Infra getrennt zu halten ist die
  zukunftssichere Boundary.
- **Nur DB-Name ändern, alles andere lassen** → Verworfen, weil der
  System-Prompt dann weiterhin "Du bist Voice" sagen würde — die Persona
  müsste sich mündlich dem Operator gegenüber als "Voice" vorstellen während die
  UI "Jarvis" zeigt. Schlechte UX, jeder voice-call wäre eine kleine
  Dissonanz.
- **Env-Var-Name unverändert lassen** (`VOICE_AGENT_TOKEN`) → Diskutiert.
  Geringerer Touch-Point-Footprint, aber langfristig confusing weil das
  Token in der DB an `Jarvis` gebunden ist. Der Operator hat sich für Umbenennung
  + Bootstrap-Fallback entschieden ("achte drauf das nichts kaputtgeht").
- **0114 Migration historisch korrigieren** (`name IN ('Boss', 'Jarvis')`
  statt `('Boss', 'Voice')`) → Verworfen. 0114 läuft beim Migration-
  Replay vor 0120, der Agent heisst zu dem Zeitpunkt noch "Voice". Ein
  Edit würde das Replay-Verhalten für Restore-Szenarien stillschweigend
  brechen. Stattdessen: ADR-038-Hinweis-Kommentar in 0114 + 0120 macht
  den Rename atomar danach.
- **Activity-Events nicht rewriten** → Verworfen weil der Operator explizit
  Rewrite verlangt hat. Argument: die 5 Events haben keine externen
  Konsumenten (kein BI-Export, kein Compliance-Audit), Konsistenz in der
  UI hat Vorrang.

## Konsequenzen

### Positiv

- Persona ↔ Infra Boundary ist jetzt im Code lesbar: "Voice" = LiveKit,
  "Jarvis" = die Persona die darauf läuft. Künftige Greps sind
  unambig.
- Der Operator spricht mit "Jarvis" in der UI, "Jarvis" stellt sich mündlich als
  Jarvis vor, Telegram-/Discord-/Notification-Events sagen "Jarvis".
- Künftige zweite Voice-Persona (z.B. ein zweiter Realtime-Worker für
  einen anderen Kontext) bekommt einen eigenen Persona-Namen ohne mit
  der Infra zu kollidieren.
- Env-Var-Rename mit Fallback ist defensiv: ein nicht-nachgezogenes
  `.env` führt nicht zu 401-Loop, sondern liest still den alten Var-Namen.
- Idempotente Migration auf ID + Name geguardet: zweiter Run ist No-op,
  fremde "Voice"-Rows (sollten sie existieren) bleiben unberührt.
- Test-Suite (47 Tests in 4 betroffenen Files) grün nach Update inkl.
  einer notwendigen `assert payload["requested_by"] == "jarvis"`
  Anpassung (vorher "voice", folgt jetzt dem slugified DB-Namen).

### Negativ

- **Historische Audit-Records überschrieben.** Die 5 `activity_events`
  vom Erstellungs-Zeitpunkt sagen jetzt "Jarvis" obwohl der Agent damals
  "Voice" hiess. Wer eine forensische DB-Restore-Vergleichsanalyse macht,
  sieht den Drift. Akzeptiert weil keine externen Audit-Konsumenten
  existieren.
- **VOICE_AGENT_TOKEN Fallback ist technische Schuld.** Der `or
  os.environ.get("VOICE_AGENT_TOKEN")` Fallback in `mc_client.py` muss
  irgendwann entfernt werden. Bis dahin könnte jemand das `.env`
  vergessen zu migrieren und der Container läuft trotzdem — was den
  Bootstrap-Schutz konterkariert (silenter Drift zwischen .env und
  compose). Sollte in 1-2 Release-Zyklen aufgeräumt werden.
- **Migration 0114 trägt einen erklärenden Kommentar aber keinen
  funktionalen Fix.** Wer 0114 isoliert liest, sieht `name IN ('Boss',
  'Voice')` und muss den ADR-038-Hinweis-Kommentar lesen um zu verstehen
  warum kein 'Jarvis' drin steht. Doc-Schuld.
- **Frontend-Komponenten heissen weiter `VoiceWidget` / `VoiceOverlay`**.
  Ein neuer Frontend-Entwickler sieht "VoiceWidget" und denkt evtl. der
  Agent heisst "Voice" — Dokumentation in ARCHITECTURE.md klärt das aber
  pro Komponenten-File gibt's keinen Hinweis.
- **Der Voice-Worker-Container-Name `voice-worker` und das voice_worker/
  Verzeichnis suggerieren weiterhin "Voice"-Persona.** Konsistente
  Boundary ist intentional (Infra = voice), aber das ist nicht
  selbsterklärend ohne den ADR.

## Referenzen

- Migration: `backend/alembic/versions/0120_rename_voice_agent_to_jarvis.py`
- Code:
  - `voice_worker/main.py` (JARVIS_INSTRUCTIONS Persona, entrypoint logs)
  - `voice_worker/mc_client.py` (JARVIS_AGENT_TOKEN + Fallback,
    JARVIS_BOARD_ID, alle Comment-Refs)
- Env: `.env` (`$HOME/.mc/secrets/mission-control/.env`),
  `docker-compose.yml` (voice-worker service env-passthrough)
- Tests (alle grün nach Rename):
  - `backend/tests/test_voice_graph_highlight.py` (5 fixtures + 1
    `requested_by` assertion)
  - `backend/tests/test_voice_worker_mc_client.py` (1 fixture + 1 comment)
  - `backend/tests/test_deliver_to_telegram.py` (3 references)
  - `backend/tests/test_vault_briefing.py` (5 `_make_agent("Jarvis", …)`)
- Docs:
  - `docs/agent-state.md` (Roster-Tabelle)
  - `docs/ARCHITECTURE.md` (Vault-Sektion + Änderungshistorie)
- Verwandte ADRs: ADR-034 (Vault as Source of Truth — definiert
  Agent-Coverage), ADR-033 (Secrets vs Credentials Boundary — selbe
  Klasse von Namensraum-Trennung), ADR-037 (mc finish Preflight —
  Geschwister-Lesson aus derselben Session)
- Live-Verify: `docker compose exec voice-worker python -c "import mc_client; …"`
  → `me status: 200, agent name: Jarvis, agent id: 156b915b-…`
