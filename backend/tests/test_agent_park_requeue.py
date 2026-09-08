"""`mc park` / agent PATCH status=inbox resets the dispatch cycle (08.09.2026).

Incident: the lead parked a worker's sub-task with `blocked` (the only status
the CLI offered) → three Blocker-Approvals within one minute. `inbox` via the
agent endpoint kept dispatched_at/ack_at, so the bridge never saw a fresh
new_task and the worker's lock stayed set. This pins the reset.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup(session, *, lead: bool):
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token
    from app.utils import utcnow

    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    session.add(board)
    raw, h = generate_agent_token()
    caller = Agent(id=uuid.uuid4(), name="Lead" if lead else "Worker", board_id=board.id,
                   agent_token_hash=h, is_board_lead=lead,
                   scopes=["tasks:read", "tasks:write", "tasks:create"])
    session.add(caller)
    worker = caller if not lead else Agent(id=uuid.uuid4(), name="Worker", board_id=board.id,
                                           is_board_lead=False, scopes=["tasks:read", "tasks:write"])
    if lead:
        session.add(worker)
    await session.flush()
    task = Task(id=uuid.uuid4(), board_id=board.id, title="Sub", status="in_progress",
                assigned_agent_id=worker.id, dispatched_at=utcnow(), ack_at=utcnow(),
                started_at=utcnow(), dispatch_attempt_id=str(uuid.uuid4()))
    session.add(task)
    worker.current_task_id = task.id
    session.add(worker)
    await session.commit()
    return board, caller, worker, task, raw


@pytest.mark.asyncio
async def test_lead_park_resets_dispatch_cycle_and_releases_worker(client, fake_redis):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, lead, worker, task, token = await _setup(s, lead=True)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "inbox"},
            headers={"Authorization": f"Bearer {token}",
                     "X-Dispatch-Attempt-Id": task.dispatch_attempt_id},
        )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        from app.models.agent import Agent
        t = await s.get(Task, task.id)
        w = await s.get(Agent, worker.id)
        assert t.status == "inbox"
        assert t.dispatched_at is None and t.ack_at is None and t.started_at is None
        assert t.run_control is None
        assert t.dispatch_attempt_id is None, "attempt id must be cleared so the bridge re-delivers"
        assert t.assigned_agent_id == worker.id, "park keeps the assignee — it is a requeue, not a reassign"
        assert w.current_task_id is None, "worker lock released so the queue drain can fire"


@pytest.mark.asyncio
async def test_worker_waiting_keeps_dispatch_cycle(client, fake_redis):
    """`mc patch --status waiting` on the own task is a live hold: nothing is reset."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, worker, _, task, token = await _setup(s, lead=False)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "waiting"},
            headers={"Authorization": f"Bearer {token}",
                     "X-Dispatch-Attempt-Id": task.dispatch_attempt_id},
        )
    assert resp.status_code == 200, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        t = await s.get(Task, task.id)
        assert t.status == "waiting"
        assert t.ack_at is not None and t.dispatched_at is not None
