"""Tests: user_test Gate nur bei Browser-relevanten Tasks."""
import uuid
import pytest
from unittest.mock import patch, AsyncMock
from sqlmodel.ext.asyncio.session import AsyncSession
from tests.conftest import test_engine
from app.models.task import Task


@pytest.mark.anyio
async def test_browser_parent_gets_user_test(make_board, make_agent, make_task):
    """Root-Task mit needs_browser=True + Children → user_test."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    parent = await make_task(
        board.id, title="Build Dashboard",
        status="review", assigned_agent_id=rex.id,
        needs_browser=True,
    )
    await make_task(board.id, title="Subtask", parent_task_id=parent.id, status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.handle_test_handoff", new_callable=AsyncMock) as mock_handoff:
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, parent.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Looks good",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "user_test", f"Expected user_test, got {task.status}"
            mock_handoff.assert_called_once()


@pytest.mark.anyio
async def test_non_browser_parent_skips_user_test(make_board, make_agent, make_task):
    """Root-Task OHNE needs_browser + Children → direkt done."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    parent = await make_task(
        board.id, title="Morning Briefing",
        status="review", assigned_agent_id=rex.id,
    )
    await make_task(board.id, title="Subtask", parent_task_id=parent.id, status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock):
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, parent.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Approved",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "done", f"Expected done, got {task.status}"
            assert task.completed_at is not None


@pytest.mark.anyio
async def test_visual_proof_gets_user_test(make_board, make_agent, make_task):
    """Root-Task mit delegation_type=visual_proof + Children → user_test."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    parent = await make_task(
        board.id, title="Redesign Page",
        status="review", assigned_agent_id=rex.id,
        delegation_type="visual_proof",
    )
    await make_task(board.id, title="Subtask", parent_task_id=parent.id, status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.handle_test_handoff", new_callable=AsyncMock) as mock_handoff:
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, parent.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Ship it",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "user_test"


@pytest.mark.anyio
async def test_single_task_goes_done(make_board, make_agent, make_task):
    """Einzeltask ohne Children → direkt done."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    task_obj = await make_task(
        board.id, title="Single Fix",
        status="review", assigned_agent_id=rex.id,
        needs_browser=True,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock):
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, task_obj.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="OK",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "done"
