# ADR-005 — Board-Lead-First Dispatch

**Status:** Accepted
**Datum:** 2026-02
**Scope:** Backend/Dispatch

## Kontext

Bei Task-Erstellung muss MC entscheiden: **Wer bekommt den Task?** Optionen:
- Erster verfügbarer Agent (Round-Robin)
- Spezialisierter Agent nach Tags/Skills (Auto-Match)
- Explizit zugewiesener Agent (falls User es gesetzt hat)
- Board Lead (Orchestrator) — der entscheidet dann selber, wen er delegiert

Ohne klare Regel verteilt sich Dispatch-Logik über den ganzen Stack und Agents machen unabgestimmt parallel.

## Entscheidung

**Board-Lead-First Dispatch** — `find_dispatch_target()` hat folgende Priorität:

1. **Explizit zugewiesen** (`assigned_agent_id` gesetzt) → dieser Agent
2. **Orchestrator** (wenn Board hat Board-Lead-Rolle) → Board Lead (Henry)
3. **Board Lead-Fallback**: Wenn kein Board Lead verfügbar → erster Agent mit Gateway-Anbindung + Warning-Event
4. **Kein Agent verfügbar** → Event + Notification an den Operator (manuelle Zuweisung)

Konsequenz: **Henry (Board Lead) sieht alle neuen Tasks** und delegiert dann explizit über `assigned_agent_id` an Worker.

Henry's System-Prompt macht das zur Pflicht-Verhalten:
- "Ich bin das Gehirn des Teams. Ich empfange Tasks und delegiere."
- "Ich kodiere NICHT selbst — nicht einmal kleine Fixes."
- "Delegation-Tabelle: Code → FreeCode, Deployment → Deployer, Review → Rex, etc."

## Alternativen

- **A: Round-Robin** → verworfen weil Agents keine gleichen Skills haben (Cody kann kein Review)
- **B: Auto-Match nach Tags/Skills** → verworfen weil Skills-Matching fragil ist, Kontext oft wichtiger als Skill-Label
- **C: Erster Online-Agent** → verworfen weil kein Audit-Trail, kein Orchestrator-Blick aufs Ganze
- **D: Pull-basiert (Agents holen sich selbst Tasks)** → verworfen weil Race Conditions, kein zentraler Entscheider

## Konsequenzen

### Positiv
- **Single Point of Decision**: Henry sieht alles, entscheidet durchdacht statt reflexartig
- **Audit Trail**: Alle Delegierungen gehen durch Henry (mit Begründung im Task-Comment)
- **Explicit > Implicit**: `assigned_agent_id` hat höchste Priorität — Henry kann jederzeit manuell steuern
- **Konsistenz**: Kein Task "verschwindet" in einem Worker ohne Henry's Blessing
- **Learning Loop**: Henry sieht Patterns (welche Agents überlastet, welche idle)

### Negativ
- **Single Point of Failure**: Henry offline → kein Dispatch (Fallback auf erste Agent mildert, aber nicht ideal)
- **Latency**: Neue Tasks warten auf Henry's Ack → +Sekunden bis Minuten
- **Henry Overload**: Bei vielen Tasks Henry's Session-Kontext wird lang — "Lost in the Middle"
- **Abhängig von Henry-Prompt**: Wenn Henry's Soul-Prompt nicht streng genug ist, versucht er Tasks selbst zu erledigen (Observed: passiert gelegentlich)

## Referenzen

- Code: `backend/app/services/dispatch.py:find_dispatch_target()`, `_find_planning_agent()`
- Henry's Soul: `backend/templates/SOUL.md.j2` + board lead soul_md in DB
- Dispatch-Message: Enthält immer "Du bist Orchestrator, kein Executor" Reminder
- Verwandt: ADR-002 (Subagent Dispatch — Henry in Haupt-Session, Workers in isolierter)
