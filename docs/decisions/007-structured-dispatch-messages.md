# ADR-007 — Structured Dispatch Messages mit Curl-Callbacks

**Status:** Accepted
**Datum:** 2026-02
**Scope:** Backend/Dispatch

## Kontext

Ursprüngliche Dispatch-Messages waren kurze, unstrukturierte Prompts:
> "Neue Task: Fixe Bug XYZ. Task-ID abc-123."

Probleme:
1. **Agent halluzinierte API-Calls**: Ohne konkrete Curl-Beispiele erfand der Agent URLs, die gar nicht existierten
2. **Kontext fehlte**: Agent wusste nicht was das Board tut, welche Konventionen gelten, welche Memories relevant sind
3. **Kein Audit Trail**: Prompts zu kurz um nachzuvollziehen was der Agent wissen sollte
4. **Agent brauchte Rückfragen**: "Wie poste ich einen Kommentar?" → zusätzliche RPC-Roundtrips
5. **Review-Kontext weg**: Bei Review-Rejection musste alles nochmal gesendet werden

## Entscheidung

**Strukturierte Dispatch-Messages** (~8000 Zeichen, erzeugt in `_build_dispatch_message()`) enthalten:

1. **Identity-Reminder**: "Du bist {agent_name}, {role}. Board: {board_name}."
2. **Task-Details**: Titel, Beschreibung, Priority, Tags, Fristen
3. **Projekt-Kontext**: Falls Task zu einem Projekt gehört → Project-Beschreibung, GitHub-URL, aktuelle Phase
4. **Board Memory**: Top 3-5 relevante Einträge (knowledge, decision, lesson) aus `board_memory`
5. **Agent Lessons**: Private Learnings des Agents die zu Task-Tags passen
6. **Intelligence**: Relevante Failure-Patterns, Agent-Performance-Warnungen
7. **Git-Sektion**: Falls Git-Workflow aktiv → Branch-Name, Workspace-Pfad, Push-Anweisung
8. **Curl-Callbacks self-contained**: Konkrete 1:1 kopierbare Befehle für ACK, Progress, Checkpoint, Completion, Blocker. Mit `$MC_API_URL` + `$MC_TOKEN` als Env-Vars (Agent hat die bereits)
9. **Comment-Protokoll**: Update/Evidence/Next Format erklärt
10. **ACK-Instruktion**: "Bestätige SOFORT mit PATCH status: in_progress — bevor du irgendwas anderes machst"
11. **Callback-Expectations**: "Bei Blockade nach 5min → status: blocked mit Begründung"

Jeder Curl-Befehl nutzt **Python-Heredoc-Pattern** statt Inline-JSON (siehe feedback_soul_md_api_calls.md):
```bash
python3 -c "import json; print(json.dumps({'content': '...', 'comment_type': 'progress'}))" > /tmp/mc_payload.json
curl -s -X POST "$MC_API_URL/..." -H "Authorization: Bearer $MC_TOKEN" --data @/tmp/mc_payload.json
```

## Alternativen

- **A: Kurze Prompts, Agent zieht Context selbst nach** → verworfen weil zusätzliche RPC-Roundtrips + Halluzinationen
- **B: Komplette TOOLS.md + SOUL.md in jeder Message** → verworfen weil zu lang (20k+ Token) + Redundanz
- **C: JSON-strukturierte Messages** → verworfen weil schwer für Agent zu lesen, verliert Natural-Language-Vorteile
- **D: Vorlage + Placeholder** → zu starr, kann nicht auf Task-spezifischen Kontext reagieren

## Konsequenzen

### Positiv
- **Halluzinations-Prevention**: Curl-Befehle sind 1:1 kopierbar, Agent rät keine URLs
- **Self-contained**: Agent braucht keine zusätzlichen Infos, kann sofort loslegen
- **Kontext-Aware**: Board Memory + Lessons + Intelligence fliessen ein
- **Audit Trail**: Dispatch-Message ist ein Artefakt (testwürdig, debuggt)
- **Template-getrieben**: `_build_dispatch_message()` ist pure function, testbar
- **Parallel-fetchbar**: Kontext wird via `asyncio.gather()` gleichzeitig geladen (Board Memory + Lessons + Intelligence), schnell

### Negativ
- **Message-Grösse**: 8000+ Zeichen pro Dispatch → "Lost in the Middle" Risiko
- **Token-Kosten**: Jeder Task verursacht 2-3k Tokens nur für den Kontext
- **Wartungsaufwand**: `_build_dispatch_message()` muss bei Template-Änderungen mit-aktualisiert werden
- **Curl-Drift**: Wenn API-Endpoints sich ändern, Messages in alten Tasks sind stale (aber nicht kritisch, Agent arbeitet neuen Task, nicht alten)
- **Inline-JSON-Gefahr**: Ursprünglich curl mit Inline `-d '...'` → brach bei Markdown-Zeichen im Prompt. Heute python3-Heredoc. Regression-Risiko bei unbedachten Änderungen.

## Referenzen

- Code: `backend/app/services/dispatch.py:_build_dispatch_message()` + `_load_dispatch_context()`
- Regression-Fix: feedback_soul_md_api_calls.md — "python3+json.dumps für POST, nie curl Inline-JSON"
- Comment-Protokoll: CLAUDE.md "Progress-Kommentar Format" + Agent Soul-Templates
- Verwandt: ADR-001 (Dispatch ACK — ist Teil der Message), ADR-004 (Board Memory Loading), ADR-002 (Subagent Dispatch — Review-Rejection reused Message)
