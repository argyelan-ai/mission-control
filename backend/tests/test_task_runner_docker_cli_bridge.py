"""Docker cli-bridge agents must NOT use the host free-code-bridge recovery path.

Before the fix, `agent_runtime in ('free-code-bridge', 'cli-bridge')` was
treated as identical. Docker cli-bridge agents (davinci, cody, rex etc.)
therefore ended up in `_handle_cli_bridge_stale_dispatch` /
`_handle_cli_bridge_inprogress_recovery` — both use
`settings.free_code_bridge_url`, which is irrelevant for Docker agents
(they poll directly via HTTP, no queue daemon).

After the fix, only the normal stale-check with role-based
_idle_threshold_for applies to Docker cli-bridge agents.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_cli_bridge_agent_with_task(session: AsyncSession, runtime: str):
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.board import Board
    from app.utils import utcnow

    board = Board(id=uuid.uuid4(), name="T", slug="t")
    session.add(board)
    agent = Agent(
        id=uuid.uuid4(),
        name="Davinci" if runtime == "cli-bridge" else "Legacy",
        board_id=board.id,
        agent_runtime=runtime,
        role="designer",
        scopes=["tasks:read", "tasks:write"],
    )
    session.add(agent)
    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Stale task",
        status="in_progress",
        assigned_agent_id=agent.id,
        started_at=utcnow() - timedelta(hours=2),  # 2h old — well past any threshold
        dispatched_at=utcnow() - timedelta(hours=2),
    )
    session.add(task)
    await session.commit()
    await session.refresh(agent)
    await session.refresh(task)
    return agent, task


async def _run_with_fake_redis(runner, runtime, fake_redis):
    from app.services.task_runner import TaskRunnerService  # noqa: F401
    from app import redis_client

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await _create_cli_bridge_agent_with_task(s, runtime=runtime)

    async def _fake_get_redis():
        return fake_redis

    from app.services import task_runner as _tr
    from app.services import sse as _sse

    mock_stale = AsyncMock()
    mock_recov = AsyncMock()
    with patch.object(runner, "_handle_cli_bridge_stale_dispatch", mock_stale), \
         patch.object(runner, "_handle_cli_bridge_inprogress_recovery", mock_recov), \
         patch.object(redis_client, "get_redis", _fake_get_redis), \
         patch.object(_tr, "get_redis", _fake_get_redis), \
         patch.object(_sse, "get_redis", _fake_get_redis):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await runner._check_dispatch_ack(s)
            await runner._check_stale_in_progress(s)

    return mock_stale, mock_recov


@pytest.mark.asyncio
async def test_docker_cli_bridge_skipped_in_stale_dispatch_recovery(fake_redis):
    """Docker cli-bridge agent must not end up in the host recovery path."""
    from app.services.task_runner import TaskRunnerService
    runner = TaskRunnerService()
    mock_stale, mock_recov = await _run_with_fake_redis(runner, "cli-bridge", fake_redis)
    mock_stale.assert_not_called()
    mock_recov.assert_not_called()


@pytest.mark.asyncio
async def test_host_free_code_bridge_still_uses_recovery(fake_redis):
    """Host free-code-bridge agent still uses the recovery path."""
    from app.services.task_runner import TaskRunnerService
    runner = TaskRunnerService()
    mock_stale, mock_recov = await _run_with_fake_redis(runner, "free-code-bridge", fake_redis)
    assert mock_stale.called or mock_recov.called
