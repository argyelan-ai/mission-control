# ADR-004 — BoardMemory als Single Knowledge-Table

**Status:** Accepted
**Datum:** 2025 (ursprünglich beim Board-Memory-Feature)
**Scope:** Backend/DB

## Kontext

Knowledge-Management in MC hat drei Dimensionen:
1. **Board Memory** — Erkenntnisse/Entscheidungen spezifisch für ein Board (z.B. "MC Dev Board: Frontend nutzt Tailwind v4")
2. **Agent Knowledge** — Private Learnings eines einzelnen Agents (z.B. "Cody: Webpack-Konfig Trick")
3. **Global Knowledge** — Team-weite Referenzen (z.B. "Design-DNA", "Security-Richtlinien")

Initial naheliegender Ansatz: **drei separate Tabellen** — `board_memory`, `agent_knowledge`, `global_knowledge`. Jede mit eigenen Routern, Queries, Types.

## Entscheidung

**Eine einzige Tabelle** `board_memory` mit Triple-Scoping über Nullable Fields:

| Scope | `board_id` | `agent_id` | Sichtbarkeit |
|---|---|---|---|
| Board Memory | SET | NULL | Alle Agents auf dem Board |
| Agent Knowledge | SET | SET | Nur dieser Agent |
| Global Knowledge | NULL | NULL | Alle Agents + UI |

Memory-Typen (enum): `knowledge`, `decision`, `lesson`, `reference`, `journal`, `concept`, `weekly_review`, `insight`, `research`.

Routers:
- `routers/memory.py` — Board-scoped API (`GET /boards/{id}/memory`) — bestehend, unverändert
- `routers/memory.py` — Global Knowledge Base API (`GET /knowledge`) — neu, mit Query-Params `?type=&agent_id=&search=`
- `routers/agent_scoped.py` — Agent-seitige Schreiboperationen

## Alternativen

- **A: Drei separate Tabellen** → verworfen weil:
  - 3× Migration-Overhead (Schema-Änderungen müssen an 3 Stellen)
  - 3× Router + 3× Types + 3× Queries
  - Jeder Feature-Add betrifft alle drei
  - Bei "aus Board-Memory ein globales Learning machen" → manuelles Copy-Move
- **B: Generische `knowledge_entries` Tabelle ohne Board-Bezug** → verworfen weil Board Memory schon existierte und Agents es aktiv nutzen
- **C: Nested JSON in Agent-Tabelle** → verworfen weil keine Volltextsuche, keine Filterung

## Konsequenzen

### Positiv
- **Single Source of Truth**: Ein Knowledge-Eintrag, ein Ort, eine ID
- **Einfache Promotion**: Board-Memory zu Global machen = `UPDATE SET board_id = NULL`, keine Datenmigration
- **Einheitliche API**: Alle Knowledge-Operationen gehen durch denselben Router
- **Query-Performance**: Ein Index auf `(board_id, agent_id, memory_type)` reicht
- **UI-Flexibilität**: Frontend kann Scope-Filter clientseitig bauen ohne 3 Endpoints
- **Agent-Dispatch**: Dispatch kann mit einer Query Board-Memory + Agent-Lessons + Global in einem Zug laden (`asyncio.gather()`)

### Negativ
- **Denormalisierung**: Semantisch unterschiedliche Dinge in einer Tabelle — konzeptionelle Unsauberkeit
- **Filter-Logik in Queries**: Jede Knowledge-Query muss Scope-Filter explizit setzen (sonst leakt man z.B. Agent-Knowledge)
- **Index-Kosten**: Mehrere Nullable-Columns → Index hat mehr Varianz
- **Permission-Checks komplexer**: Ein User darf Board-Memory eines anderen Boards nicht sehen → muss im Router geprüft werden, Schema erzwingt es nicht

## Referenzen

- Model: `backend/app/models/memory.py` (`BoardMemory`)
- Routers: `backend/app/routers/memory.py`
- Frontend: `frontend-v2/src/app/memory/page.tsx` (Tab "Board Memory" vs "Knowledge Base")
- Dispatch-Integration: `backend/app/services/dispatch.py:_load_dispatch_context()`
