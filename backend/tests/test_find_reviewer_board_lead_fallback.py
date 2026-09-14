"""Incident: Review-Karte landet beim Board-Lead statt bei Rex.

Karte 09dc3c11 ("Nachzuegler PR #500") ging an Boss (Board-Lead), nicht an
Rex, weil find_reviewer() ueber find_agent_by_role()'s Board-Lead-Fallback
zurueckkehrt, bevor der eigene Namens-Fallback (der Rex per role=None
gefiltert sowieso nie erreicht haette) ueberhaupt laeuft.

Reproduziert exakt die Board-Konstellation aus dem Vorfall: Rex traegt eine
Freitext-Rolle ("Review & Security Expert - Code-Reviews, PRs, Sicherheit,
Qualitaetssicherung") statt des Enum-Werts "reviewer", weil das Modell die
Rolle nur bei Neu-Anlage validiert - Altdaten mit Freitext bleiben in der DB.
Der Test schreibt die Freitext-Rolle deshalb per rohem UPDATE (bypass der
Pydantic-Validierung), genau wie die echte Zeile in der Produktions-DB.
"""
import uuid

import pytest
from sqlalchemy import update as _sa_update

from app.models.agent import Agent
from app.scopes import AgentRole


async def _set_freetext_role(session, agent_id: uuid.UUID, freetext: str) -> None:
    """Write a role value the Pydantic validator would reject on create.

    Mirrors how legacy rows end up with freetext role: SQLAlchemy ORM
    reconstruction from a DB row does not re-run field_validator(mode="before"),
    so a raw UPDATE is the only way to reproduce the real broken state.
    """
    await session.exec(
        _sa_update(Agent).where(Agent.id == agent_id).values(role=freetext)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_find_reviewer_finds_rex_not_board_lead_when_role_is_freetext(
    session, make_board, make_agent,
):
    """Vorfall 09dc3c11: Rex hat Freitext-Rolle, Boss ist Board-Lead.

    find_reviewer() muss Rex liefern (Namens-Fallback), NIEMALS den
    Board-Lead — auch nicht als find_agent_by_role()'s eingebauten
    Board-Lead-Fallback, der fuer eine Reviewer-Suche die falsche Antwort ist.
    """
    board = await make_board()
    rex = await make_agent(name="Rex", role=None, board_id=board.id)
    await _set_freetext_role(
        session, rex.id,
        "Review & Security Expert — Code-Reviews, PRs, Sicherheit, Qualitätssicherung",
    )
    boss = await make_agent(
        name="Boss", role=None, board_id=board.id, is_board_lead=True,
    )
    await _set_freetext_role(session, boss.id, "Board Lead — Orchestrierung, Planung")

    from app.services.work_context import find_reviewer
    result = await find_reviewer(session, board.id)

    assert result is not None
    assert result.id == rex.id, (
        f"find_reviewer lieferte {result.name if result else None} "
        f"({result.id if result else None}), erwartet Rex ({rex.id}) — "
        f"nicht Boss ({boss.id})"
    )


@pytest.mark.asyncio
async def test_find_reviewer_returns_none_without_silent_board_lead_when_no_reviewer_exists(
    session, make_board, make_agent,
):
    """Kein Reviewer auf dem Board (weder Rolle noch Name) → None, NICHT Board-Lead.

    Eine Karte darf nicht stillschweigend beim Board-Lead (und damit in
    Marks Freigabe-Inbox) landen, nur weil find_agent_by_role() intern
    einen Board-Lead-Fallback fuer die normale Dispatch-Logik hat. Der
    Aufrufer (handle_review_handoff) behandelt None bereits korrekt als
    sichtbaren "kein Reviewer zugewiesen"-Zustand statt stiller Eskalation.
    """
    board = await make_board()
    boss = await make_agent(
        name="Boss", role=None, board_id=board.id, is_board_lead=True,
    )
    await _set_freetext_role(session, boss.id, "Board Lead — Orchestrierung, Planung")
    # Kein Agent mit "rex"/"review" im Namen, keiner mit role="reviewer"

    from app.services.work_context import find_reviewer
    result = await find_reviewer(session, board.id)

    assert result is None, (
        f"find_reviewer lieferte {result.name if result else None} statt None — "
        f"Karte waere still beim Board-Lead gelandet"
    )
