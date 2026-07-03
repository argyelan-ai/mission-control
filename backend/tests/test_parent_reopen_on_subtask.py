"""Parent reopen flow: a new subtask under a review parent resets the parent back to in_progress.

Real case: Boss approved a phase → parent moves to review. Boss then delegates
follow-up work via POST /tasks to Davinci. The parent would otherwise stay stuck
on review even though new sub-work is running underneath.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlmodel import select

from app.models.task import Task, TaskEvent


@pytest.mark.asyncio
async def test_reopen_helper_promotes_review_parent_to_in_progress(async_session, board_with_agents):
    """reopen_parent_for_new_subtask sets a review parent to in_progress."""
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

    # Event was logged
    events = (await async_session.exec(
        select(TaskEvent).where(TaskEvent.task_id == parent.id)
    )).all()
    assert any(e.reason == "parent_reopened_for_new_subtask" for e in events)


@pytest.mark.asyncio
async def test_reopen_helper_noop_on_in_progress(async_session, board_with_agents):
    """Parent is already in_progress -> helper does nothing."""
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
    """Parent is done -> helper does nothing (caller must raise 422)."""
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
    """If the parent has completed_at set (leftover from an old done state) -> clear it on reopen."""
    from app.services.task_lifecycle import reopen_parent_for_new_subtask
    from app.utils import utcnow

    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    parent = Task(
        board_id=board.id, title="Feature X", status="review",
        assigned_agent_id=boss.id,
        completed_at=utcnow(),  # old leftover
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        await reopen_parent_for_new_subtask(async_session, parent.id)
    await async_session.commit()

    refreshed = await async_session.get(Task, parent.id)
    assert refreshed.completed_at is None
