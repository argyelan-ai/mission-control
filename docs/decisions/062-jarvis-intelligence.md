# ADR-062 — Jarvis Intelligence: Frontier-Delegation, Agent-Dispatch, Morgenbriefing

**Status:** Accepted
**Datum:** 2026-07-10
**Scope:** jarvis_core (frontier/tools/persona/mc_client) · voice_worker · Backend/Services · Backend/Config · Backend/Router (vault) · Infra/Compose · Docs

## Kontext

Jarvis (ADR-038/061) ist ein Concierge: Tasks aufnehmen, Status melden, Wissen
abrufen. Er kann bisher aber (1) keine schweren Fragen beantworten (die Realtime-/
Text-Modelle sind auf schnelle Konversation optimiert, nicht auf Analyse/Planung),
(2) einen Agenten nicht direkt loslegen lassen (nur `create_task` als
Backlog-Eintrag an Boss), und (3) kein proaktives Morgenbriefing liefern. Welle C
macht Jarvis in diesen drei Punkten intelligent — ohne die bestehende Voice-/
Telegram-Erfahrung oder die Auth-Grenzen zu verletzen.

## Entscheidung

### C1 — `ask_frontier`-Tool (Frontier-Delegation)

Neues Modul `jarvis_core/frontier.py`: `ask_frontier(question, context_hint)` und
das geteilte `complete_text(...)` delegieren schwere Fragen an ein starkes OpenAI-
Textmodell (ein einzelner Chat-Completions-Aufruf ohne Tools). Bewusst über
`httpx` direkt — exakt wie `jarvis_core.brain` (ADR-061), kein neues SDK.

- **Modellwahl** per Env `JARVIS_FRONTIER_MODEL`, sonst `DEFAULT_FRONTIER_MODEL`.
  Am 10.07.2026 lieferte `GET /v1/models` mit dem Operator-Key u.a. `gpt-5.6-luna/
  sol/terra`, `gpt-5.5-pro`, `gpt-5.5`, `gpt-5.4-pro`, `gpt-5-pro`, `gpt-4.1`,
  `gpt-4o`, `o3`, `o1`. Default = **`gpt-5.5`**: das neueste klar benannte,
  allgemein verfügbare Flaggschiff. Bewusst NICHT die `-pro`-Stufe (sehr hohe
  Reasoning-Latenz, sprengt das 120s-Concierge-Budget) und NICHT die
  5.6-Codenamen (luna/sol/terra unklar spezialisiert).
- **Fallback-Kette:** konfiguriertes/Default-Modell → bei Aufruf-Fehler einmalig
  `gpt-4o` → sonst ehrlicher Fehler an Jarvis (der ihn narrativiert).
- **Timeout** großzügig (120s). Persona: bei komplexen Fragen kurz ansagen
  („einen Moment, ich denk kurz nach"), Antwort in eigenen Worten komprimiert
  wiedergeben, nicht wie ein Dokument vorlesen.
- Verfügbar auf **beiden** Kanälen (Voice + Telegram).

### C2 — `dispatch_to_agent`-Tool (Agent sofort loslegen lassen)

Neues Tool + `mc_client.dispatch_to_agent(agent_name, instruction, priority)`.
Es erstellt einen Task und weist ihn dem genannten Agenten zu — der **normale
MC-Dispatch-Flow** greift dann: `POST /api/v1/agent/boards/{board}/tasks` mit
`assigned_agent_id != creator` triggert im agent-scoped Router (`agent_create_task`)
direkt den CLI-Bridge-Dispatch (Session-Start des Agenten). **Kein neuer Endpoint,
kein Auth-Bypass, keine Direkt-DB, kein Schreiben in Sessions.** Das `dispatch`-Feld
der Backend-Antwort (`{status: dispatched|blocked|not_dispatched|...}`) wird
durchgereicht, damit Jarvis ehrlich sagen kann, ob der Agent wirklich losgelegt hat.

Abgrenzung zu `create_task` (in beiden Tool-Beschreibungen deutlich gemacht):
- `create_task` = **Backlog-Eintrag**; unbekannter/fehlender Assignee → Fallback
  auf Board Lead (Boss) zur Orchestrierung.
- `dispatch_to_agent` = **sofort loslegen**; `agent_name` ist Pflicht, Fuzzy-Match
  (wiederverwendet `_resolve_agent_id`), aber **KEIN** stiller Board-Lead-Fallback
  — unbekannter Name → klarer Fehler, damit Jarvis nachfragt statt falsch zu raten.
- **Mindestqualität:** `instruction` < 50 Zeichen wird im Tool abgewiesen
  (`instruction_too_thin`) — ein Agent, der sofort loslegt, würde bei einer zu
  dünnen Anweisung Fehlarbeit produzieren; Jarvis fragt stattdessen nach.

### Sicherheit — Pre-Dispatch-Gating gilt auch für `dispatch_to_agent`

Ein `dispatch_to_agent`-Task ist ein **parentloser Root-Task mit expliziter
Fremd-Zuweisung durch einen Nicht-Board-Lead-Agent** (Jarvis). Die bisherige
Gating-Bedingung (`agent_task_status.py`) stufte nur **Sub-Tasks** (`parent_task_id
is not None`) als „executable work item" ein — parentlose Tasks umgingen das
Pre-Dispatch-Gating (`evaluate_promote_decision`, `HIGH_RISK_TAGS`,
`autonomy_level`) komplett. Damit hätte Jarvis einen Agenten an einem
High-Risk-Task (z.B. `db`/`migration`/`security`) ohne Risk-/Autonomy-Bewertung
starten können.

**Fix:** Die Klassifikation ist in die reine Funktion
`dispatch_gating.is_executable_work_item(...)` extrahiert (kein Fake-`parent_task_id`-
Hack). Ein Task ist executable, wenn er einer **anderen** Person zugewiesen ist UND
(Sub-Task ODER Ersteller ist **nicht** Board Lead). Damit läuft auch der
parentlose Jarvis-Dispatch durch das volle Gating; ist es gated on, wird ein
solcher Task konservativ als `planning` behandelt (auto-promote nur bei klaren
Low-Risk-Signalen), und Jarvis meldet den `dispatch_status` ehrlich.

Unverändert (durch Regressionstests abgesichert): Board-Lead-Sub-Task-Delegation
bleibt gated, Board-Lead-**Root**-Delegation bleibt ungated (der Board Lead ist der
Orchestrator), der Sub-Task-Pfad bleibt gated, self-assigned/unassigned bleibt
ungated. Gating ist per `enable_dispatch_gating` (Default false) geschaltet — bei
Default-Konfiguration ändert sich nichts, der Fix schließt den Bypass für den
gehärteten Betrieb.

### C3 — Tägliches Morgenbriefing (Backend-Job)

Neuer Hintergrund-Loop `app.services.jarvis_briefing.jarvis_briefing_loop`,
gestartet in der FastAPI-Lifespan nach demselben Muster wie `_vault_lint_loop`/
`_vault_decay_loop` (`create_tracked_task`, Cancel im Shutdown).

- Täglich zur Zeit `JARVIS_BRIEFING_HOUR` (Default `06:30` Europe/Zurich), gated
  auf `JARVIS_BRIEFING_ENABLED` (Default false) + `OPENAI_API_KEY`.
- Aggregiert die `/agent/vault/briefing`-Daten (frischer V1.5-Pfad: age_days,
  staleness, dedup) über den geteilten `mc_client` (Self-Call mit Jarvis-Token),
  formatiert sie mit `format_briefing_as_context` und lässt das Frontier-Modell
  (C1-Codepfad) einen kompakten deutschen Briefing-Text schreiben.
- Schreibt das Ergebnis als **Vault-Note** `Morgenbriefing YYYY-MM-DD` (durable)
  und cacht es in **Redis** (`mc:jarvis:briefing:{date}`, 36h). Der Redis-Key ist
  zugleich der **Idempotenz-Guard** (SET NX → max. 1 Lauf/Tag; bei Fehler wird der
  Guard freigegeben, damit ein späterer Lauf erneut versucht) und der **lag-freie
  Read-Path**.
- Der `/agent/vault/briefing`-Endpoint liefert das heutige generierte Briefing als
  `generated_briefing` (+ `generated_briefing_date`) mit. Jarvis' `briefing`-Tool/
  Persona: existiert ein heutiges generiertes Briefing → dieses bevorzugt
  wiedergeben; sonst Live-Daten + ehrlicher Hinweis „ein generiertes Morgenbriefing
  von heute gibt es nicht".

## Alternativen

- **`ask_frontier` über das `openai`-SDK** → Verworfen (neue schwergewichtige
  Dependency + Lock-Regeneration), `httpx` reicht — konsistent mit ADR-061.
- **`dispatch_to_agent` als neuer Endpoint / Direkt-Session-Write** → Verworfen.
  Der bestehende agent-scoped Task-Endpoint dispatcht bei Zuweisung bereits; ein
  Zweitweg wäre ein Auth-/Lifecycle-Bypass (vgl. `no_second_lifecycle`).
- **Morgenbriefing als DB-`ScheduledJob`** → Verworfen. Das Feature ist env-gegatet
  (nicht DB-konfiguriert) und braucht keinen Job-Datensatz; der env-gegatete
  Hintergrund-Loop (wie vault_lint/decay) ist die passendere, seedfreie Konvention.
- **Briefing-Note per Vault-Search zurücklesen** → Verworfen zugunsten des
  Redis-Read-Paths (kein Kompaktierungs-Lag, keine Snippet-Truncation, saubere
  „heute?"-Prüfung). Die Vault-Note bleibt der durable Record.

## Konsequenzen

### Positiv
- Jarvis kann echte Analyse/Planung liefern (delegiert, in eigenen Worten),
  Agenten unmittelbar starten und proaktiv briefen — ohne neue Auth-Pfade.
- Eine Quelle der Wahrheit: Frontier-Codepfad von Tool + Briefing geteilt; Tools
  auf beiden Kanälen identisch.
- Default-off (Briefing) + Fallback-Kette (Frontier) → Bestandsverhalten und reine
  GHCR-Images ohne Config bleiben unberührt.

### Negativ
- Der `ask_frontier`-Tool-Handler liest `OPENAI_API_KEY`/`JARVIS_FRONTIER_MODEL`
  aus dem Environment (jarvis_core kennt keine Backend-Settings). In Produktion
  reicht docker-compose beide als echte Env-Vars an Backend + voice_worker durch;
  ein reines lokales `.env` ohne Export würde den Tool-Pfad still deaktivieren
  (der Briefing-Job dagegen übergibt `settings.*` explizit).
- Ein weiterer env-gegateter Hintergrund-Loop im Backend (geringe Kosten, Cancel
  im Shutdown gehandhabt).

## Nicht-Ziele (Welle C)

Keine proaktive Push-Meldung („Cody ist fertig") — nur die synchrone Bestätigung
beim Dispatch. Kein Frontier-Tool-Use (nur Text-Delegation). Kein UI.
