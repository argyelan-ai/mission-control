# ADR-033 — Secrets vs Credentials: Boundary kodifizieren statt unifizieren

**Status:** Accepted
**Datum:** 2026-05-13 (accepted 2026-05-14)
**Scope:** Backend/DB · Backend/Auth · Agent Protocol · UX/Settings

## Kontext

Mission Control hat heute zwei separate Tabellen + Router fuer geheime Werte. Beide nutzen dieselbe Fernet-Verschluesselung (`services/encryption.py`), aber unterschiedliche Schemas, Auth-Modelle und Konsumenten. Historisch sind sie unabhaengig entstanden — `secrets` zuerst fuer System-Tokens, `credentials` spaeter fuer den UI-Vault — und die Aehnlichkeit der Namen hat zu wiederkehrender Verwirrung gefuehrt.

### Heutige Realitaet

| Aspekt | `secrets` | `credentials` |
|---|---|---|
| **Tabelle** | `secrets` | `credentials` |
| **Identifier** | unique `key: str` (z.B. `openai_api_key`) | `id: UUID` + nicht-eindeutiger `name: str` |
| **Value-Shape** | einzelner String (Fernet-encrypted) | JSON-Blob `{username, password}` / `{token}` / `{content}` |
| **Type-System** | flach | typed: `login` \| `token` \| `custom` |
| **Write-Auth** | `Role.ADMIN` (POST/PATCH/DELETE) | `require_user` — jeder eingeloggte User |
| **Agent-scoped Endpoints** | keine — agents koennen weder lesen noch schreiben | `GET /api/v1/agent/boards/{board_id}/credentials[/{id}]` mit Scope `credentials:read` |
| **UI** | Settings → Secrets / API-Keys (admin-only) | Settings → Credentials Tab (user-managed) |
| **Konsumenten** | `gateway_secrets_sync` (LLM-Provider-Keys → OpenClaw global), `secrets_helper` (per-agent `.env` Provider-Key), `deploy.py`, `publish_adapters.py`, `newsletter_service.py`, `workflow_renderer.py`, `workflow_validator.py` | `agent_scoped.py` (visual-verify login, agent vault list/get), `linkedin_poster.py`, `task_context_builder.py` |
| **Typische Eintraege** | 1 pro Provider/Service: openai_api_key, anthropic_api_key, openclaw_token, discord_bot_token, github_token | N pro Use-Case: medewo-login, twitter-bearer, livekit-api-key, xai-api-key, custom-notes |

### Inzident 2026-05-13 (Voice-Foundation)

Der Operator legt 3 Voice-Foundation Secrets im UI an (`xai_api_key`, `livekit_api_key`, `livekit_api_secret`) → landen in `credentials`-Tabelle (UI-Tab heisst "Credentials"). Boss bekommt vom Brief: "Secrets sind unter `/api/v1/secrets`" → POST schlaegt mit 401 fehl (Admin-only, agents haben keinen User-JWT). Der Operator und Henry beziehen sich wechselseitig auf "die Secrets" ohne klar zu sein **welche** Tabelle gemeint ist. Drei Eskalationen bis das Routing geklaert war (Sparky-Scope-Creep → Boss-Auth-Check → Operator Manual-Trigger im UI).

Der eigentliche Bug ist nicht die Existenz zweier Stores — es ist das **Fehlen einer klaren Grenze** und entsprechender Doku in SOUL.md/TOOLS.md.

### Was sich aus dem Code ergibt

Die beiden Stores adressieren tatsaechlich unterschiedliche Probleme:

- **`secrets`** ist die "Infrastructure Token Wallet": ein globaler Schluessel pro externem Dienst. Backend-Services nutzen ihn, um im Namen des Operators mit OpenAI/Anthropic/GitHub/OpenClaw zu sprechen. Agents brauchen ihn nie direkt — sie bekommen die abgeleiteten Werte via `.env` (Sparky) oder OAuth-Bootstrap (claude-Fleet).
- **`credentials`** ist der "Task-Time Vault": typed (login/token/custom), multi-instance, jeder User darf schreiben, Agents lesen Scope-protected. Verwendung: Agent automatisiert irgendeine Website / API im Auftrag des Operators und braucht dafuer Anmeldedaten oder einen API-Token.

Anders gesagt: `secrets` = "wie MC selbst mit der Welt redet". `credentials` = "was MC den Agents zum Erledigen einer Task in die Hand drueckt".

## Entscheidung

**Beide Tabellen bleiben getrennt.** Wir kodifizieren ihre Rollen statt sie zu unifizieren:

- **`secrets`** = **System Token Wallet** (alleinige Wahrheit fuer Infrastructure-Tokens).
  - Genau 1 Eintrag pro Provider/Dienst (`openai_api_key`, `anthropic_api_key`, `github_token`, `discord_bot_token`, `openclaw_token`).
  - Schreiben/Aendern/Loeschen ausschliesslich durch den Operator (Role.ADMIN).
  - Backend-Services greifen via `secrets_helper.get_secret_plaintext_by_id()` oder `get_secret_by_key()` zu.
  - Agents haben **keinen** API-Zugriff (kein agent-scoped Endpoint, kein Tooling).
  - Auto-Sync in den OpenClaw Gateway (`gateway_secrets_sync`) bleibt Admin-Only.

- **`credentials`** = **Agent Task Vault** (alleinige Wahrheit fuer Task-Time Geheimnisse).
  - N Eintraege pro Use-Case, frei benennbar.
  - Typed: `login` (mit `url`-Pflichtfeld), `token`, `custom`.
  - Schreiben durch jeden eingeloggten User (der Operator via UI).
  - Agents lesen via `GET /api/v1/agent/boards/{board_id}/credentials[/{id}]` mit Scope `credentials:read`.
  - Wenn ein Agent einen neuen Credential anlegen muesste: aktuell **kein** agent-scoped POST — der Operator legt manuell im UI an. Falls dieser Workflow staut, wird ein eigener agent-scoped POST mit zusaetzlichem Scope `credentials:write` als Follow-up-ADR geprueft.

### Begleitende Doku-Updates (im Scope dieses ADRs, in Follow-Up-Commits umzusetzen)

1. **SOUL.md.j2 / TOOLS.md.j2**: Sektion "Secrets vs Credentials" mit klarer Entscheidungstabelle und beiden Curl-Beispielen. Agents bekommen explizit: "Du hast Zugriff auf credentials (Task-Vault). Secrets sind System-Tokens, die du nie direkt brauchst — der Backend-Service holt sie fuer dich".
2. **UI-Labels**: Settings-Sidebar trennt visuell "API Keys" (= secrets) von "Credentials Vault" (= credentials). Heutiger Tab-Titel "Credentials" bleibt; "Secrets"-Tab bekommt Untertitel "System Tokens (Admin only)".
3. **Task-Brief-Templates** (in `dispatch.py` `_build_dispatch_message`): wenn ein Task einen Credential braucht, immer **mit credential_id**, nie mit "key". Beispiel-Curl referenziert nur den agent-scoped Endpoint.
4. **CLAUDE.md**: Kurzregel "Brauchst du ein System-Token (LLM-Provider, GH, Discord)? → `secrets`. Brauchst du Login/Token fuer eine Task-spezifische Aktion (Website, externer API, Trading-Account)? → `credentials`."

## Alternativen

- **Alternative A — Unifizieren: eine `secrets`-Tabelle, beide Use-Cases reinmergen.**
  Verworfen weil:
  - Different Schemas: `secrets.key` ist unique, `credentials.name` nicht. Eintrag-Merge waere lossy.
  - Different Auth-Modelle: Admin-write fuer System-Tokens ist eine bewusste Sicherheitsgrenze (kein User kann openai_api_key versehentlich ueberschreiben). Eine geteilte Tabelle muesste pro-Eintrag-Auth einfuehren — komplexer als zwei Tabellen.
  - Agent-Read-Scope `credentials:read` darf nicht auf System-Tokens ausgedehnt werden (Sparky soll niemals an `openclaw_token` rankommen, sonst kann er das Gateway umkonfigurieren).
  - Migration waere riskant: 23 secrets-Eintraege + 11 credentials-Eintraege live, jeder Konsument muesste angefasst werden.

- **Alternative B — Credentials abschaffen, alles in `secrets` pressen.**
  Verworfen weil: bricht typed credential_type (login/token/custom), entfernt die wertvolle Trennung von username/password als JSON, und der Operator verliert die UI-Komfortzone (er legt im Voice-Foundation-Flow eindeutig "Credentials" im UI an, nicht "API-Keys").

- **Alternative C — Secrets abschaffen, alles in `credentials` pressen.**
  Verworfen weil: die Konsumenten-Liste auf `secrets` ist gross (gateway_secrets_sync, deploy, publish_adapters, newsletter, workflow_*). Migration waere ein Mehrtage-Projekt mit Risiko fuer Content-Pipeline + Gateway-Sync. Vor allem aber: per-Provider eindeutiger `key` (`openai_api_key`) ist ein praktisches Invariante — ein Tippfehler in einem Eintragsnamen sollte nicht zu zwei "OpenAI Keys" fuehren.

- **Alternative D — `secrets` als View ueber `credentials`.**
  Verworfen weil: Auth-Boundary (Admin vs User) liesse sich auf einer View nur per Filter abbilden — verschiebt das Problem ins App-Layer ohne echten Gewinn.

## Konsequenzen

### Positiv

- **Klare semantische Grenze:** "Hat es einen `key` (system-managed, einmalig pro Service)? → `secrets`. Hat es einen `name` + `credential_type` (task-managed, multi-instance)? → `credentials`."
- **Bestehende Auth-Boundary bleibt:** Sparky kann nie an `openclaw_token`, weil der Pfad `agent-scoped` ueber `credentials:read` fuehrt und Secrets dort schlicht nicht existieren.
- **Keine Migration noetig:** Alle 23 `secrets`-Eintraege + 11 `credentials`-Eintraege bleiben wo sie sind.
- **Agent-Briefe werden eindeutig:** SOUL.md / TOOLS.md / dispatch.py-Templates referenzieren ab sofort den richtigen Endpoint.

### Negativ

- **Namensaehnlichkeit bleibt:** "secrets" und "credentials" klingen weiter aehnlich. Mitigation: UI-Labels schaerfen ("API Keys" vs "Credentials Vault"), CLAUDE.md-Eintrag, klare Doku in SOUL.md.
- **Keine zentrale Audit-Sicht:** Wer ein neues Geheimnis im System anlegen will, muss wissen welche Tabelle. Mitigation: Settings-UI zeigt beide Tabs an einem Ort, mit Untertitel.
- **Wenn Agents irgendwann selbst Credentials anlegen muessen:** wir brauchen einen neuen agent-scoped POST mit `credentials:write` Scope (Follow-Up-ADR). Aktuell schwerwiegend nur fuer Boss bei Self-Service-Onboarding-Flows.

### Was nicht passiert

- Keine Datenbank-Migration.
- Keine API-Breaking-Changes — alle bestehenden Endpoints behalten Pfad + Auth.
- Keine Aenderung am `secret_id`-Feld auf der `agents`-Tabelle (per-Agent Runtime-Provider-Key bleibt via secrets-Tabelle adressiert).

## Referenzen

- Betroffene Dateien:
  - `backend/app/models/secret.py` — `Secret` model
  - `backend/app/models/credential.py` — `Credential` model
  - `backend/app/routers/secrets.py` — Admin-only CRUD + provider templates
  - `backend/app/routers/credentials.py` — User CRUD
  - `backend/app/routers/agent_scoped.py:3352-3421` — agent-scoped credentials GET
  - `backend/app/services/secrets_helper.py` — `get_secret_plaintext_by_id`
  - `backend/app/services/gateway_secrets_sync.py` — Provider-Key Sync zum OpenClaw Gateway
  - `backend/templates/SOUL.md.j2`, `backend/templates/TOOLS.md.j2` — Doku-Update (Follow-up)
- Verwandte ADRs:
  - [ADR-006](006-jinja2-template-source-of-truth.md) — Templates als SoT, betrifft die SOUL.md / TOOLS.md Doku-Updates
  - [ADR-009](009-agent-scoped-router-separat.md) — Agent-Scoped Router separat
  - [ADR-027](027-universal-agent-runtime-binding.md) — `agent.secret_id` Runtime-Bindung
- Inzident-Memory: `~/.claude/projects/-Users-Henry/memory/project_open_bugs_mc_agent_observability.md` (Bug 8)
