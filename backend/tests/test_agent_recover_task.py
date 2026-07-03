"""/agent/me/recover-task — recovery endpoint for poll-runtime agents.

After a container/host restart during an active task, the tmux/claude
session is gone, but the DB status stays `in_progress`. poll.sh can no
longer paste the prompt, because /agent/me/poll only returns `state=working`.
The recovery endpoint resets the task to inbox → the next poll delivers it
as `new_task` with a fresh prompt.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_agent_with_task(session: AsyncSession, status: str = "in_progress", runtime: str = "cli-bridge"):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.auth import generate_agent_token
    from app.utils import utcnow

    board = Board(id=uuid.uuid4(), name="Test", slug="t")
    session.add(board)
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name="Researcher",
        board_id=board.id,
        agent_runtime=runtime,
        status="idle",
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    session.add(agent)
    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Research: something",
        status=status,
        assigned_agent_id=agent.id,
        dispatched_at=utcnow(),
        ack_at=utcnow(),
        started_at=utcnow(),
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task, raw_token


@pytest.mark.asyncio
async def test_recover_resets_in_progress_task_to_inbox(client: AsyncClient):
    """Task in_progress → recovery resets to inbox + clears dispatch tracking."""
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        task_id = task.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recovered"] is True
    assert data["task_id"] == str(task_id)
    assert data["previous_status"] == "in_progress"

    # DB: task is back to inbox, dispatch tracking cleared
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task_id)
        assert refreshed.status == "inbox"
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None
        assert refreshed.started_at is None


@pytest.mark.asyncio
async def test_recover_idempotent_when_no_active_task(client: AsyncClient):
    """No active task → recovered=False, no error."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Setup: agent without in_progress task (task is 'done')
        _, agent, task, token = await _setup_agent_with_task(s, status="done")

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recovered"] is False
    assert data["reason"] == "no_active_task"


@pytest.mark.asyncio
async def test_recover_leaves_system_comment(client: AsyncClient):
    """Recovery posts a system comment for the audit trail."""
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s)
        task_id = task.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        recovery_comments = [c for c in comments if c.comment_type == "system" and "Recovery" in c.content]
        assert len(recovery_comments) == 1
        assert "re-dispatched" in recovery_comments[0].content.lower() or "wird re-dispatched" in recovery_comments[0].content


@pytest.mark.asyncio
async def test_recover_works_for_host_runtime(client: AsyncClient):
    """Host-runtime agents (Boss) need recovery the same way after a launchd restart."""
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress", runtime="host")
        task_id = task.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recovered"] is True

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task_id)
        assert refreshed.status == "inbox"


@pytest.mark.asyncio
async def test_recover_clears_run_control_stopped(client: AsyncClient):
    """Recovery must clear run_control=stopped — otherwise the agent deadlocks.

    Scenario: task was 'stopped' by the user, was later manually reassigned,
    ran again, agent tried status=review → backend blocked because of
    run_control=stopped. Recovery must also reset run_control.
    """
    from app.models.task import Task
    from app.utils import utcnow

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")
        # Simulate stopped run control
        t = await s.get(Task, task.id)
        t.run_control = "stopped"
        s.add(t)
        await s.commit()
        task_id = t.id

    resp = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["recovered"] is True

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task_id)
        assert refreshed.status == "inbox"
        assert refreshed.run_control is None, "run_control muss gecleared sein"


@pytest.mark.asyncio
async def test_recover_rate_limited_after_recent_recovery(client: AsyncClient):
    """Protection against poll.sh crash loop: two recoveries in <60s → second is rejected.

    If the first recovery sets a task to inbox and the next poll cycle claims
    the task again (status=in_progress), a second recovery call must not
    immediately set it back to inbox — otherwise an infinite loop results.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")

    # First recovery: successful
    r1 = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.json()["recovered"] is True

    # Simulate: task was claimed again in the meantime (inbox → in_progress)
    from app.models.task import Task
    from app.utils import utcnow
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.status = "in_progress"
        t.dispatched_at = utcnow()
        t.ack_at = utcnow()
        s.add(t)
        await s.commit()

    # Second recovery within 60s: must be rate-limited
    r2 = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    data = r2.json()
    assert data["recovered"] is False
    assert data["reason"] == "rate_limited"
    assert "last_recovery_at" in data

    # Task stays in_progress — not reset back to inbox
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.status == "in_progress"


@pytest.mark.asyncio
async def test_recover_then_poll_delivers_new_task(client: AsyncClient):
    """Full recovery flow: recover → poll returns new_task with a prompt."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, agent, task, token = await _setup_agent_with_task(s, status="in_progress")

    recover = await client.post(
        "/api/v1/agent/me/recover-task",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert recover.status_code == 200
    assert recover.json()["recovered"] is True

    poll = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert poll.status_code == 200
    data = poll.json()
    assert data["state"] == "new_task"
    assert "task" in data
    assert data["task"]["id"] == str(task.id)
    assert "prompt" in data["task"] and len(data["task"]["prompt"]) > 0
