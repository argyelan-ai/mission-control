"""Tests fuer Task-Dependency-Enforcement beim Dispatch."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── dependencies_met() Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dependencies_met_no_deps(make_board, make_task):
    """Task ohne Dependencies → True."""
    board = await make_board()
    task = await make_task(board_id=board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        from app.services.dispatch import dependencies_met
        assert await dependencies_met(s, refreshed) is True


@pytest.mark.asyncio
async def test_dependencies_met_all_done(make_board, make_task):
    """Alle Dependencies done → True."""
    board = await make_board()
    dep_task = await make_task(board_id=board.id, title="Dep Task", status="done")
    task = await make_task(board_id=board.id, title="Blocked Task")

    from app.models.task import TaskDependency
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskDependency(task_id=task.id, depends_on_task_id=dep_task.id))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        from app.services.dispatch import dependencies_met
        assert await dependencies_met(s, refreshed) is True


@pytest.mark.asyncio
async def test_dependencies_met_not_done(make_board, make_task):
    """Dependency nicht done → False."""
    board = await make_board()
    dep_task = await make_task(board_id=board.id, title="Dep Task", status="in_progress")
    task = await make_task(board_id=board.id, title="Blocked Task")

    from app.models.task import TaskDependency
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskDependency(task_id=task.id, depends_on_task_id=dep_task.id))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        from app.services.dispatch import dependencies_met
        assert await dependencies_met(s, refreshed) is False


@pytest.mark.asyncio
async def test_dependencies_met_partial(make_board, make_task):
    """Eine von zwei Dependencies nicht done → False."""
    board = await make_board()
    dep1 = await make_task(board_id=board.id, title="Dep 1", status="done")
    dep2 = await make_task(board_id=board.id, title="Dep 2", status="inbox")
    task = await make_task(board_id=board.id, title="Blocked Task")

    from app.models.task import TaskDependency
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskDependency(task_id=task.id, depends_on_task_id=dep1.id))
        s.add(TaskDependency(task_id=task.id, depends_on_task_id=dep2.id))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        from app.services.dispatch import dependencies_met
        assert await dependencies_met(s, refreshed) is False


# ── Dispatch Blocking Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_blocked_by_dependency(client, make_board, make_agent, make_task):
    """Task mit unerfuellter Dependency wird NICHT dispatcht."""
    board = await make_board(auto_dispatch_enabled=True)
    agent = await make_agent(
        board_id=board.id, is_board_lead=True,     )
    dep_task = await make_task(board_id=board.id, title="Phase 1", status="in_progress")
    task = await make_task(board_id=board.id, title="Phase 2 Task", status="inbox")

    from app.models.task import TaskDependency
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskDependency(task_id=task.id, depends_on_task_id=dep_task.id))
        await s.commit()

    with patch('app.services.dispatch.logger') as mock_rpc, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch.engine", test_engine):

        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task.id, board.id)

    # chat_send darf NICHT aufgerufen worden sein
    mock_rpc.chat_send.assert_not_called()

    # Task bleibt unveraendert (kein dispatched_at, kein assigned_agent_id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(type(task), task.id)
        assert refreshed.dispatched_at is None


