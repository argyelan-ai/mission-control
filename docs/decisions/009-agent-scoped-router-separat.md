# ADR-009 — Agent-Scoped Router separat von User-Router

**Status:** Accepted
**Datum:** 2025 (bei Agent-Auth Feature)
**Scope:** Backend/Auth

## Kontext

In MC gibt es zwei fundamentalshierlich verschiedene API-Konsumenten:
- **Operator (User)**: Greift über das Frontend auf alle Resources zu (Tasks, Agents, Boards, Projekte). Role-basiertes Auth (admin/operator/viewer).
- **Agents (Programm)**: Updaten ihre eigenen Tasks, lesen Board-Memory, posten Kommentare. Scope-basiertes Auth (16 fine-grained Scopes).

Frage: **Ein gemeinsamer Router mit dual auth, oder zwei separate Router?**

## Entscheidung

**Zwei separate Router**:
- `backend/app/routers/agents.py` — User-facing: CRUD, Provisioning, Config. `require_user()` Dependency.
- `backend/app/routers/agent_scoped.py` — Agent-facing: `/api/v1/agent/*` Endpoints. `require_agent()` + `require_scope()` Dependencies.

Beispiel: `PATCH /api/v1/tasks/{id}` (user-facing, in `tasks.py`) und `PATCH /api/v1/agent/boards/{board_id}/tasks/{task_id}` (agent-facing, in `agent_scoped.py`) sind **verschiedene Endpoints** mit unterschiedlicher Logik — auch wenn sie beide einen Task updaten.

**Agent-Auth**: PBKDF2-Hash (200k iterations) in `agents.agent_token_hash`. Token einmalig beim `POST /agents` zurückgegeben, danach nur Hash in DB.

**Scope-Gating**: Jeder agent-scoped Endpoint hat `Depends(require_scope(Scope.TASKS_WRITE))`. Ohne Scope → 403.

**Zusätzlich**: TOOLS.md-Rendering filtert Sektionen nach erlaubten Scopes — Agent sieht Curl-Beispiele nur für Endpoints die er auch nutzen darf.

## Alternativen

- **A: Ein Router mit dual auth Dependency** → verworfen weil:
  - Code wird unübersichtlich: jede Route muss wissen "bin ich gerade User oder Agent?"
  - Scope-Checks und Role-Checks mischen sich → Sicherheits-Risiko
  - Agent könnte versehentlich user-only Endpoints erreichen wenn Dep falsch konfiguriert
- **B: Router-Präfix als einziges Unterscheidungsmerkmal** → verworfen weil:
  - Keine echte Isolation, leicht übersehbar
  - Entwickler denkt "Ich erweitere mal das Endpoint" und fügt Sicherheitslücke ein
- **C: GraphQL statt REST** → verworfen weil Team-Expertise REST-basiert, zu grosse Migration

## Konsequenzen

### Positiv
- **Klare Trennung**: "Ist das eine User- oder Agent-Operation?" immer offensichtlich
- **Sicherheits-Isolation**: Agent-Endpoints können nie versehentlich User-Scopes bekommen (und umgekehrt)
- **Scope-Checks an einer Stelle**: `require_scope()` Dependency ist self-contained
- **Test-Isolation**: Agent-Tests können User-Routes komplett ignorieren
- **TOOLS.md-Generation**: Ein Template durchlaufen, nur relevante Sektionen rendern
- **Audit-Logs klarer**: "agent-request" vs "user-request" direkt am URL-Präfix erkennbar

### Negativ
- **Code-Duplikation**: Manche Endpoints existieren 2x (einmal user, einmal agent) mit minimal unterschiedlicher Logik
- **Dokumentationspflicht**: Frontend-Entwickler + Agent-Prompts müssen wissen welches Endpoint zu nutzen ist
- **Mehr Routes in Swagger-Docs**: Gefühlt doppelte Menge
- **Consistency-Risk**: Wenn user-endpoint was ändert, agent-endpoint muss nachziehen (oder explizit anders sein)

## Referenzen

- User-Router: `backend/app/routers/agents.py`, `tasks.py`, `boards.py`
- Agent-Router: `backend/app/routers/agent_scoped.py`
- Scopes: `backend/app/scopes.py` (16 Scopes, `require_scope()` Factory, `DEFAULT_SCOPES` pro Rolle)
- Auth-Impl: `backend/app/auth.py` (`require_user`, `require_agent`, PBKDF2 verify)
- TOOLS.md-Filter: `backend/app/routers/agents.py:_generate_tools_md()` + Template
- Verwandt: ADR-010 (PBKDF2-Cache), ADR-006 (Template rendering für TOOLS.md)
