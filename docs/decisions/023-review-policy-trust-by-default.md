# ADR-023 — Review-Policy: Trust-by-Default + Reflection-Decoupling

**Status:** Accepted
**Datum:** 2026-04-20
**Scope:** Backend/Agent-Protocol · SOUL.md · Board-Config
**Supersedes:** — (verfeinert ADR-005, ADR-007)

## Kontext

Bisher hat MC zwei orthogonale Mechanismen als einen einzigen Gate
behandelt:

1. **`boards.require_review_before_done`** — Hart kodiertes Gate: jeder
   Task auf `mc-dev` MUSS durch `review` bevor er auf `done` darf. Rex
   (Reviewer) wird fuer jede Aenderung gerufen — auch fuer Typo-Fixes,
   Renames, Log-Zeilen.
2. **Pflicht-Reflexion** (4-Feld Self-Reflexion vor Task-Abschluss) — ist
   der Learning-Loop: `Lesson` aus der Reflexion fliesst nach Qdrant
   und wird beim naechsten aehnlichen Dispatch im Kontext des Agents
   wieder hervorgeholt.

Das Problem: im bisherigen Code war die Reflexion an
`require_review_before_done=True` gekoppelt. Wer also das Board-Flag
auf `False` stellt, verliert **beides** — Rex-Review UND den Team-
Learning-Loop. Umgekehrt zwingt der harte Board-Flag Rex zu "Pipifax"-
Reviews (Typos, Doku-Edits), die Aufwand kosten ohne Qualitaets-
gewinn, und stresst die Agent-Queue mit unnoetigem Round-Trip.

Der Wunsch des Operators: "Rex soll nicht ueber jeden Typo gucken muessen. Der
Dev soll entscheiden, wann Review wirklich noetig ist — und die
Verantwortung dafuer tragen. Aber das Reflektieren darf NICHT
weggehen — das ist unser Team-Gedaechtnis."

## Entscheidung

**Zwei getrennte Hebel, klar entkoppelt:**

### 1. Review-Gate (Board-Flag) — Trust-by-Default

`boards.require_review_before_done = False` fuer `mc-dev`.

- Developer/Tester/Deployer entscheiden **selbst**, ob ein Task ueber
  `review` laeuft oder direkt auf `done` geht.
- Rex wird **opt-in** gerufen — via `mc review` wenn der Dev es fuer
  noetig haelt.
- Policy-Text steht in `SOUL.md.j2` (shared block `role != "orchestrator"`):
  - **`review` Pflicht:** Code auf `main` (Feature/Bugfix/Refactor),
    neue API/Schema-Aenderung, Security-relevant, unsicher ob Loesung
    stimmt, User-Test erforderlich.
  - **`done` direkt OK:** Housekeeping, reversible Fixes (Typo, Log),
    Research/Analyse-Tasks, Agent-lokale Config.
  - **Faustregel:** Im Zweifel `review`.

Boards die einen harten Review-Gate brauchen (z.B. `production-deploy`
Board spaeter) koennen das Flag weiter auf `True` setzen — die Code-
Logik bleibt erhalten.

### 2. Reflexion (Config-Flag) — Immer Pflicht

`app.config.Settings.enforce_reflection = True` (Default).

- Entkoppelt vom Board-Flag. Gilt fuer **jede** Closing-Transition
  (`in_progress → review` oder `in_progress → done`).
- Board Leads (Orchestratoren) sind ausgenommen — sie implementieren
  nicht, sie koordinieren.
- 4 Pflichtfelder (`REFLECTION_REQUIRED_FIELDS` in `app/constants.py`),
  mindestens 80 Zeichen (`REFLECTION_MIN_CHARS`).
- Guard prueft: "existiert eine `comment_type=reflection` vom Agent auf
  dem Task?" — nicht mehr "ist das der letzte Kommentar?". Progress-
  Updates nach der Reflexion sind damit unproblematisch.

### 3. Guard-Refactor (`routers/agent_scoped.py`)

```python
_is_closing_transition = (
    new_status in ("review", "done")
    and task.status not in ("review", "user_test")
)
_reflection_required = (
    _cfg.enforce_reflection
    and _is_closing_transition
    and not agent.is_board_lead
)
```

Rule-2 (board review-gate) bleibt als eigener Guard bestehen — nur
seine Abhaengigkeit zur Reflexion ist aufgehoben.

## Konsequenzen

### Positiv

- **Weniger Rex-Last:** Nur noch Tasks die wirklich reviewt werden
  muessen landen bei Rex. Typo-Fixes, Doku, Renames laufen direkt auf
  `done`.
- **Schnellere Turnarounds:** Dev macht Task fertig → `done` in einem
  Schritt, kein Rex-Round-Trip bei trivialen Aenderungen.
- **Learning-Loop bleibt intakt:** Jede Closing-Transition triggert
  die Reflexion → Qdrant-Index → Retrieval beim naechsten Dispatch.
- **Klar kommunizierte Verantwortung:** SOUL.md sagt explizit, wann
  `review` Pflicht ist. Dev weiss, was erwartet wird.
- **Per-Board-Opt-in:** Boards die Hard-Gate brauchen, setzen das
  Flag einfach auf `True`.

### Negativ / Risiken

- **Judgement-Call beim Dev:** Wer zu schnell `done` drueckt, riskiert
  dass Bugs unreviewt auf `main` landen. Mitigation: klare Policy-
  Regeln in SOUL, "im Zweifel review".
- **Rueckwaerts-Kompat:** Boards die vorher auf das gekoppelte Verhalten
  setzten (kein Board-Flag → keine Reflexion) bekommen jetzt die
  Reflexions-Pflicht. Default `enforce_reflection=True` macht das
  explizit. Opt-out via ENV-Variable moeglich.

### Verifikation

1. `UPDATE boards SET require_review_before_done = false WHERE slug = 'mc-dev';`
2. SOUL.md.j2 Review-Policy-Block fuer `role != "orchestrator"` hinzugefuegt.
3. Alle 4 Worker-Agents (FreeCode, Sparky, Tester, Deployer) reprovisioniert.
4. Tests: 1020 backend-Tests passen. `test_predone_validation.py`,
   `test_task_events.py`, `test_workflow_scenarios.py` um explizite
   Reflection-Posts ergaenzt.

## Referenzen

- `backend/app/routers/agent_scoped.py` — Rule-4 Guard (entkoppelt)
- `backend/app/config.py:49` — `enforce_reflection: bool = True`
- `backend/app/constants.py` — `REFLECTION_REQUIRED_FIELDS`, `REFLECTION_MIN_CHARS`, `REFLECTION_CHARTER`
- `backend/templates/SOUL.md.j2` — Review-Policy + Self-Reflexion Blocks
- ADR-005 — Board-Lead-First Dispatch (unveraendert)
- ADR-007 — Structured Dispatch Messages (unveraendert)
