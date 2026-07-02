"""Parent-Reopen-Flow: Neuer Subtask unter review-Parent setzt Parent zurueck auf in_progress.

Real-Case: Boss approved Phase → Parent geht auf review. Danach delegiert Boss eine
Follow-up-Arbeit via POST /tasks an Davinci. Parent haette sonst auf review gehaengt
obwohl unten eine neue Subarbeit laeuft.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlmodel import select

from app.models.task import Task, TaskEvent


@pytest.mark.asyncio
async def test_reopen_helper_promotes_review_parent_to_in_progress(async_session, board_with_agents):
    """reopen_parent_for_new_subtask setzt review-Parent auf in_progress."""
    from app.services.task_lifecycle import reopen_parent_for_new_subtask

    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    parent = Task(
        board_id=board.id, title="Feature X", status="review",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        reopened = await reopen_parent_for_new_subtask(
            async_session, parent.id, new_subtask_title="Neues Konzept"
        )
    await async_session.commit()

    assert reopened is True
    refreshed = await async_session.get(Task, parent.id)
    assert refreshed.status == "in_progress"

    # Event wurde geloggt
    events = (await async_session.exec(
        select(TaskEvent).where(TaskEvent.task_id == parent.id)
    )).all()
    assert any(e.reason == "parent_reopened_for_new_subtask" for e in events)


@pytest.mark.asyncio
async def test_reopen_helper_noop_on_in_progress(async_session, board_with_agents):
    """Parent ist schon in_progress -> helper macht nix."""
    from app.services.task_lifecycle import reopen_parent_for_new_subtask

    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    parent = Task(
        board_id=board.id, title="Feature X", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        reopened = await reopen_parent_for_new_subtask(async_session, parent.id)

    assert reopened is False


@pytest.mark.asyncio
async def test_reopen_helper_noop_on_done(async_session, board_with_agents):
    """Parent ist done -> helper macht nix (Caller muss 422 raisen)."""
    from app.services.task_lifecycle import reopen_parent_for_new_subtask

    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    parent = Task(
        board_id=board.id, title="Feature X", status="done",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        reopened = await reopen_parent_for_new_subtask(async_session, parent.id)

    assert reopened is False
    refreshed = await async_session.get(Task, parent.id)
    assert refreshed.status == "done"  # unchanged


@pytest.mark.asyncio
async def test_reopen_helper_clears_completed_at(async_session, board_with_agents):
    """Wenn Parent completed_at hat (alter done-Restbestand) -> clearen beim reopen."""
    from app.services.task_lifecycle import reopen_parent_for_new_subtask
    from app.utils import utcnow

    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    parent = Task(
        board_id=board.id, title="Feature X", status="review",
        assigned_agent_id=boss.id,
        completed_at=utcnow(),  # altes Residuum
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        await reopen_parent_for_new_subtask(async_session, parent.id)
    await async_session.commit()

    refreshed = await async_session.get(Task, parent.id)
    assert refreshed.completed_at is None
