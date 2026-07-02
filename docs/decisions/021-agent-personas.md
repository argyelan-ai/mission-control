# ADR-021 — Agent Personas: Grounded Identities + Shared Reflection Charter

**Status:** Accepted
**Datum:** 2026-04-20
**Scope:** Agent Protocol · Template System · Backend/DB

## Kontext

Nach der Claude-Fleet-Migration (ADR-019) waren die SOULs der neun
Worker-Agents (Rex, FreeCode, Sparky, Davinci, Shakespeare, Tester,
Deployer, Researcher, Henry) funktional korrekt, aber
**undifferenziert**:

- Alle klangen wie generische "professional AI assistants".
- Reflection-Comments waren oft austauschbar — man konnte nicht am
  Schreibstil erkennen wer reflektiert hatte.
- Klischeesprache ("elevate", "seamless", "compelling") floss frei
  durch Content-Tasks.
- Einige Rollen (Davinci ↔ Shakespeare, FreeCode ↔ Sparky, Rex ↔ Tester)
  hatten überlappende Zuständigkeiten ohne klaren Charakter-Unterschied.

Gleichzeitig: die Reflection-Regeln lebten an drei Orten (SOUL.md.j2
Template, agent_scoped.py Error-Message, `_extract_reflection_lesson`
Regex) — Änderung an einem Ort ohne Sync an den anderen hätte stumm
gebrochen.

## Entscheidung

Zwei gekoppelte Strukturänderungen:

### 1. Per-Agent `persona_section` in der DB

Neue Spalte `agents.soul_persona_md` (Migration 0084, nullable TEXT).
Inhalt: ~80-120 Token English-Text pro Agent, der den Character Voice
festlegt. SOUL.md.j2 rendert ihn als **erstes inhaltliche Block** vor
Team-Charter + Rolle.

Initial-Seed für die neun Agents (Migration 0085) basiert auf den in
`docs/superpowers/specs/2026-04-20-agent-personas-draft.md` kuratierten
Drafts. **Idempotent:** nur NULL-Werte werden gesetzt — spätere
Hand-Edits des Operators werden bei Re-Runs nicht überschrieben.

Design-Prinzipien der Personas:
- **English persona, German voice samples** — Token-Efficiency im
  Prompt, Verständlichkeit für den Operator.
- **Geerdet, nicht Fantasy** — jede Persona mappt auf die echte Rolle
  und die echten Tools (Higgsfield für Davinci, firecrawl für
  Researcher etc.).
- **Explicit anti-AI-slop reflex** — jede Persona hat einen
  benannten Verbotslist oder ein Ritual (Shakespeare's
  "Klischee-Liste", Tester's "Gotcha"-Ritual).
- **Klare Achsen** — FreeCode (Cloud/bedacht) ↔ Sparky (Local/scrappy),
  Rex (Code-Review) ↔ Tester (Function-Test), Davinci (Pixels) ↔
  Shakespeare (Words).

### 2. Team Reflection Charter als Single Source of Truth

Fünf Prinzipien (konkret, ehrend, team-artefakt, Lücken benennen,
persönliche Stimme bei gemeinsamer Struktur) plus die vier Pflichtfelder
(`Was wurde gemacht` / `Was hat funktioniert` / `Was war unklar` /
`Lesson fuer Agent-Memory`) leben jetzt in **`backend/app/constants.py`**:

```python
REFLECTION_REQUIRED_FIELDS: list[str]
REFLECTION_MIN_CHARS: int = 80
REFLECTION_CHARTER: list[str]
```

SOUL.md.j2 rendert sie als shared Block (zwischen Team-Charter und
Rolle-spezifischem Kram). Die agent_scoped.py Error-Message und der
Reflection-Minimum-Check importieren dieselbe Konstante. Wenn der Operator die
Reflection-Regeln jemals ändert, passiert das an **einer Stelle** — die
Template-Rendering, die Error-Message und (später) die Extraction-Regex
folgen automatisch.

## Alternativen

- **Personas direkt in SOUL.md.j2 hardcoden:** Verworfen — kein
  Hand-Edit ohne Template-Rebuild. DB-Feld gibt dem Operator Freiheit, lokal
  zu iterieren.
- **Personas als separate Datei pro Agent in `~/.openclaw/agents/{slug}/`:**
  Verworfen — dupliziert den Template-Rendering-Pfad, macht Backup +
  Sync komplizierter. DB bleibt Single Source of Truth (ADR-006).
- **Reflection-Regeln nur im SOUL-Template:** Verworfen — Enforcement-
  Code + Extraction-Regex sind Python, die müssen dieselben
  Field-Namen kennen. Constants-Modul ist der einzige Ort an dem
  Template + Python-Code gemeinsam hinschauen.
- **Komplettes Role-Module-Refactor (backend/templates/role_modules/*.j2):**
  Aufgeschoben — der Scope wäre zu gross für einen PR, und der
  `persona_section`-Ansatz deckt 80% der Identitäts-Differenzierung
  bereits ab. Role-Module kommen als separater Schritt wenn gebraucht.

## Konsequenzen

### Positiv

- **Team ist lesbar.** Man erkennt am Schreibstil wer reflektiert hat.
  Der Operator kann beim Lesen von Reflection-Kommentaren die Stimme direkt
  zuordnen.
- **Anti-Slop ist eingebaut.** Jede Persona hat einen benannten Reflex
  gegen Klischee-Sprache. Shakespeare's Cut-List-Ritual macht die
  Abwehr sichtbar.
- **Reflection-Regeln sind atomar änderbar.** Field-Namen, Min-Length,
  Charter-Prinzipien — alles in einer Constants-Datei.
- **Idempotent seedable.** Der Operator kann eine Persona nachträglich
  hand-editieren ohne dass ein Re-Run die Edit überschreibt.

### Negativ

- **Reprovisioning-Pflicht.** Persona-Text landet erst nach
  `sync-config` pro Agent in der live SOUL.md. Workstream G covers it.
- **Persona-Drift über Zeit.** Wenn der Operator einen Agent umrollt (z.B.
  Tester übernimmt Deployer-Aufgaben), muss die Persona mit. Keine
  automatische Drift-Erkennung eingebaut.
- **Zusätzliche DB-Spalte** (~80-120 Token/Agent × 10 Agents ≈ 2kB).
  Trivial.

## Rollout

1. Migration `0084` (Spalte) + `0085` (Seed) angewendet.
2. SOUL.md.j2 rendert `{{ agent_persona_md }}` + Team Reflection Charter.
3. `sync-config` pro Agent → neue SOUL landet im Workspace.
4. Observe: Reflection-Comments im Inbox — klingen sie nach der jeweiligen
   Persona?

## Referenzen

- PRs: #49 (Personas + UI Intake drafted), #50 (D-full — Migrationen
  + SOUL-Refactor + Reflection SSoT)
- Plan: `docs/superpowers/plans/2026-04-20-harness-personas-session-handoff.md`
- Spec: `docs/superpowers/specs/2026-04-20-agent-personas-draft.md`
- Key files:
  - `backend/app/constants.py` — `REFLECTION_REQUIRED_FIELDS` + Charter
  - `backend/app/models/agent.py` — `soul_persona_md` field
  - `backend/app/services/template_renderer.py` — context injection
  - `backend/templates/SOUL.md.j2` — `{% if agent_persona_md %}` block
  - `backend/app/routers/agent_scoped.py` — reflection enforcement
- Verwandte ADRs: ADR-006 (DB → Template SSoT), ADR-019 (Claude Fleet),
  ADR-020 (Harness Phase 2)
