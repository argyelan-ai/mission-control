"""PR #584 review W4: `claude_code_runner.dispatch_to_claude_code` used
`agent.workspace_path` as the subprocess `cwd` and then overwrote
`task.workspace_path` with it — discarding whatever git worktree or
Phase-C plain dir `task_context_builder.prepare_agent_workspace_for_task`
had already prepared and written into `task.workspace_path` earlier in the
same dispatch (dispatch.py -> dispatch_delivery._deliver_dispatch ->
dispatch_to_claude_code). The third resolution site the PR left
unaligned with the other two (task_context_builder, cli_bridge_runner).
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from tests.conftest import test_engine


def _board(**kw) -> Board:
    return Board(
        id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
        auto_dispatch_enabled=False, **kw,
    )


async def _seed(*objs):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for o in objs:
            s.add(o)
        await s.commit()
        for o in objs:
            await s.refresh(o)


@pytest.mark.asyncio
async def test_claude_code_uses_the_centrally_prepared_worktree_not_agent_base():
    """task.workspace_path already holds the git worktree
    `task_context_builder` prepared before dispatch — dispatch_to_claude_code
    must use it as `cwd`, not fall back to `agent.workspace_path` (the
    agent's shared base dir, no task-specific worktree)."""
    from app.services.claude_code_runner import dispatch_to_claude_code

    board = _board()
    agent = Agent(
        id=uuid.uuid4(), name="ClaudeDev", role="developer",
        agent_runtime="claude-code", workspace_path="/tmp/mc-test-ws/claudedev",
    )
    prepared_worktree = "/tmp/mc-test-ws/claudedev/mission-control/.worktrees/task-x"
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Ad-hoc Backend-Fix",
        status="in_progress", assigned_agent_id=agent.id,
        workspace_path=prepared_worktree,
    )
    await _seed(board, agent, task)

    fake_proc = MagicMock()
    fake_proc.pid = 4242

    with patch(
        "app.services.claude_code_runner.asyncio.create_subprocess_exec",
        new_callable=AsyncMock, return_value=fake_proc,
    ) as mock_exec, patch(
        "app.services.claude_code_runner.create_tracked_task",
        side_effect=lambda coro, name=None: coro.close(),
    ), patch(
        "app.services.claude_code_runner.emit_event", new_callable=AsyncMock,
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            loaded_agent = await s.get(Agent, agent.id)
            loaded_task = await s.get(Task, task.id)
            result = await dispatch_to_claude_code(loaded_agent, loaded_task, "prompt", s)

    assert result is True
    assert mock_exec.await_args.kwargs["cwd"] == prepared_worktree, (
        "must spawn Claude Code inside the centrally prepared worktree, "
        "not the agent's shared base workspace"
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reloaded = await s.get(Task, task.id)
        assert reloaded.workspace_path == prepared_worktree, (
            "must not clobber the prepared worktree with agent.workspace_path"
        )


@pytest.mark.asyncio
async def test_claude_code_falls_back_to_agent_workspace_when_task_has_none():
    """Regression guard the other direction: a claude-code agent dispatched
    outside the normal prepare-workspace path (task.workspace_path still
    unset) must keep falling back to agent.workspace_path, as before."""
    from app.services.claude_code_runner import dispatch_to_claude_code

    board = _board()
    agent = Agent(
        id=uuid.uuid4(), name="ClaudeDev2", role="developer",
        agent_runtime="claude-code", workspace_path="/tmp/mc-test-ws/claudedev2",
    )
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Kein vorbereiteter Workspace",
        status="in_progress", assigned_agent_id=agent.id,
    )
    await _seed(board, agent, task)
    assert task.workspace_path is None

    fake_proc = MagicMock()
    fake_proc.pid = 4243

    with patch(
        "app.services.claude_code_runner.asyncio.create_subprocess_exec",
        new_callable=AsyncMock, return_value=fake_proc,
    ) as mock_exec, patch(
        "app.services.claude_code_runner.create_tracked_task",
        side_effect=lambda coro, name=None: coro.close(),
    ), patch(
        "app.services.claude_code_runner.emit_event", new_callable=AsyncMock,
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            loaded_agent = await s.get(Agent, agent.id)
            loaded_task = await s.get(Task, task.id)
            result = await dispatch_to_claude_code(loaded_agent, loaded_task, "prompt", s)

    assert result is True
    assert mock_exec.await_args.kwargs["cwd"] == agent.workspace_path
