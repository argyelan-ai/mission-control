# ADR-012 — `use_alter=True` für Zyklus-ForeignKeys

**Status:** Accepted
**Datum:** 2025 (beim Current-Task-Tracking)
**Scope:** Backend/DB

## Kontext

Im DB-Schema gibt es zirkuläre Abhängigkeiten:

1. **Agent ↔ Task**:
   - `Agent.current_task_id → Task.id` (der Task an dem Agent gerade arbeitet)
   - `Task.assigned_agent_id → Agent.id` (welcher Agent den Task hat)

2. **Board ↔ Project**:
   - `Board.default_project_id → Project.id` (Default-Project für neue Tasks)
   - `Project.board_id → Board.id` (zu welchem Board gehört das Project)

SQLAlchemy versucht bei INSERT automatisch die richtige Reihenfolge zu finden (erst Parent, dann Child). Bei Zyklen scheitert das:
```
sqlalchemy.exc.CircularDependencyError: Can't sort tables in Agent/Task insert order
```

Beim Ersten-Setup müssen aber Agent UND Task existieren können, ohne dass einer "zuerst" da ist.

## Entscheidung

**`ForeignKey(..., use_alter=True)`** auf den Seiten die den Zyklus schliessen:

```python
# Agent.current_task_id → Task.id
current_task_id: Optional[UUID] = Field(
    default=None,
    sa_column=Column(ForeignKey("tasks.id", use_alter=True, name="fk_agent_current_task")),
)

# Board.default_project_id → Project.id
default_project_id: Optional[UUID] = Field(
    default=None,
    sa_column=Column(ForeignKey("projects.id", use_alter=True, name="fk_board_default_project")),
)
```

`use_alter=True` bedeutet: Der Foreign Key wird nach dem CREATE TABLE als separates ALTER TABLE angelegt (statt inline). Beim INSERT werden die FK-Checks deferred — SQLAlchemy weiss dann, dass die Reihenfolge nicht kritisch ist.

## Alternativen

- **A: Nullable FKs akzeptieren ohne use_alter** → verworfen weil SQLAlchemy auch bei nullable FKs Circular Dependencies erkennt und beim INSERT scheitert
- **B: Zyklus auflösen durch Join-Tabelle** → verworfen weil:
  - Join-Tabelle `agent_task_assignments` wäre essentiell eine 1:1 mit Agent+Task
  - Query-Komplexität steigt (immer JOIN nötig für "current task")
  - Mehr Indizes, mehr Writes
- **C: Deferred Constraints in Postgres** → verworfen weil:
  - DB-Spezifisch (SQLite für Tests würde brechen)
  - Weniger explizit, schwerer zu debuggen
- **D: Einen der beiden FKs weglassen** → verworfen weil:
  - `current_task_id` ist wichtig für "welche Tasks sind gerade belegt?"
  - `assigned_agent_id` ist wichtig für "welcher Agent arbeitet am Task?"
  - Beide Richtungen werden aktiv genutzt

## Konsequenzen

### Positiv
- **INSERT-Reihenfolge egal**: Alembic-Migrations können Agent + Task in beliebiger Reihenfolge erstellen
- **Kleine Änderung**: Nur 2 FKs betroffen, minimal-invasiv
- **Postgres + SQLite-kompatibel**: Funktioniert in beiden (SQLite für Tests)
- **Performance-neutral**: FK-Check passiert beim COMMIT, nicht beim INSERT

### Negativ
- **Ein separater ALTER TABLE pro FK**: Migration ist etwas länger
- **Expliziter Constraint-Name nötig** (`name="fk_..."`): Ohne wird Postgres' generierter Name verwendet, der bei Alembic-Autogenerate Probleme machen kann
- **Mental-Overhead**: Entwickler muss wissen warum diese FKs "anders" sind
- **Nicht Idiomatic**: SQLModel/SQLAlchemy-Standard ist inline FK — `use_alter` ist Speziallösung

## Referenzen

- Model-Definitionen: `backend/app/models/agent.py` (current_task_id), `backend/app/models/board.py` (default_project_id)
- Migrations die diese FKs einführen: `backend/alembic/versions/` (siehe 0013/0014 Bereich)
- SQLAlchemy Docs: [use_alter Documentation](https://docs.sqlalchemy.org/en/20/core/constraints.html#sqlalchemy.schema.ForeignKey.params.use_alter)
