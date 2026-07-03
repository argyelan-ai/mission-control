"""Tests for the dependency check in the /agent/me/poll endpoint.

Regression: 2026-04-22 — a worker polling a subtask claimed it into
in_progress even though its depends_on tasks weren't done yet. Root cause
was that agent_poll() filtered the SQL query only on status='inbox', not on
dependencies_met(). The fix now iterates the candidates and takes the
first one with satisfied dependencies.
"""

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_board_and_worker():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Dep", slug=f"dep-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=worker_id, name="PollWorker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
provision_status="provisioned",
        ))
        await s.commit()

    return board_id, worker_id, token_raw


@pytest.mark.asyncio
async def test_poll_skips_inbox_task_with_unmet_dependency(client, fake_redis):
    """Worker polls, its inbox task waits on a not-yet-done predecessor → state=idle."""
    from app.models.task import Task, TaskDependency

    board_id, worker_id, token = await _setup_board_and_worker()

    upstream_id = uuid.uuid4()
    blocked_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Predecessor that is NOT done
        s.add(Task(
            id=upstream_id, board_id=board_id, title="Upstream",
            status="in_progress",  # ← not done
        ))
        # Task that is waiting — as inbox assigned to worker
        s.add(Task(
            id=blocked_id, board_id=board_id, title="Waits on upstream",
            status="inbox", assigned_agent_id=worker_id,
        ))
        s.add(TaskDependency(task_id=blocked_id, depends_on_task_id=upstream_id))
        await s.commit()

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Must NOT have been claimed
    assert body["state"] == "idle", f"Expected idle, got {body['state']} for task {body.get('task', {}).get('id')}"

    # Task stays inbox in DB
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, blocked_id)
        assert t.status == "inbox"
        assert t.ack_at is None


@pytest.mark.asyncio
async def test_poll_claims_inbox_task_when_dependency_is_done(client, fake_redis):
    """Once the predecessor is done, poll may claim the task."""
    from app.models.task import Task, TaskDependency

    board_id, worker_id, token = await _setup_board_and_worker()

    upstream_id = uuid.uuid4()
    ready_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=upstream_id, board_id=board_id, title="Upstream done",
            status="done",  # ← satisfied
        ))
        s.add(Task(
            id=ready_id, board_id=board_id, title="Ready to run",
            status="inbox", assigned_agent_id=worker_id,
        ))
        s.add(TaskDependency(task_id=ready_id, depends_on_task_id=upstream_id))
        await s.commit()

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task"
    assert body["task"]["id"] == str(ready_id)


@pytest.mark.asyncio
async def test_poll_skips_blocked_picks_next_ready(client, fake_redis):
    """Two inbox tasks: one blocked, one free → poll takes the free one."""
    from app.models.task import Task, TaskDependency

    board_id, worker_id, token = await _setup_board_and_worker()

    upstream_id = uuid.uuid4()
    blocked_id = uuid.uuid4()
    free_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=upstream_id, board_id=board_id, title="Upstream",
            status="in_progress",
        ))
        # Blocked (older)
        s.add(Task(
            id=blocked_id, board_id=board_id, title="Blocked task",
            status="inbox", assigned_agent_id=worker_id,
        ))
        s.add(TaskDependency(task_id=blocked_id, depends_on_task_id=upstream_id))
        # Free (younger, no deps)
        s.add(Task(
            id=free_id, board_id=board_id, title="Free task",
            status="inbox", assigned_agent_id=worker_id,
        ))
        await s.commit()

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task"
    assert body["task"]["id"] == str(free_id), (
        "poll muss den freien Task nehmen, nicht den blockierten"
    )
