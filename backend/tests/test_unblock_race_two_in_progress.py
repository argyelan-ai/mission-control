"""W2-B review fix CRITICAL B-2: unblock race → two in_progress tasks.

Race: the agent claims a NEW inbox task right after the blocked-parking
grace window expires; seconds later the lead unblocks the OLD blocked task.
Without the fix the old task went back to in_progress → TWO in_progress
tasks for one agent, and poll's active-query (order_by updated_at desc)
surfaced the WRONG one: the just-unblocked old task (freshest updated_at,
ack_at still set from before the block) shadowed the task the real session
was running.

Fix, two parts:
  (a) resolve_unblock_action returns "requeue" when the assigned agent is
      alive but occupied with a different task → the unblocked task goes
      back to inbox (dispatched_at/ack_at reset) and is re-delivered by the
      normal claim flow after the current work — no interrupt.
  (b) poll's active-task resolution prefers agent.current_task_id (set at
      claim/ACK) before falling back to updated_at ordering.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from tests.conftest import test_engine


async def _setup(session: AsyncSession):
    board = Board(name="Race Board", slug=f"race-{uuid.uuid4().hex[:8]}", blocker_triage_minutes=15)
    session.add(board)
    await session.commit()
    await session.refresh(board)

    lead_raw, lead_hash = generate_agent_token()
    lead = Agent(
        name="Boss",
        role="lead",
        board_id=board.id,
        agent_token_hash=lead_hash,
        is_board_lead=True,
        scopes=["tasks:read", "tasks:write", "tasks:manage"],
    )
    session.add(lead)

    worker_raw, worker_hash = generate_agent_token()
    worker = Agent(
        name="Sparky",
        role="developer",
        board_id=board.id,
        agent_token_hash=worker_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write"],
        last_seen_at=dt.datetime.now(tz=dt.timezone.utc),  # alive
    )
    session.add(worker)
    await session.commit()
    await session.refresh(lead)
    await session.refresh(worker)

    now = dt.datetime.now(tz=dt.timezone.utc)
    old_task = Task(
        board_id=board.id,
        assigned_agent_id=worker.id,
        title="Old blocked task",
        status="blocked",
        dispatched_at=now - dt.timedelta(hours=1),
        ack_at=now - dt.timedelta(hours=1),  # acked before it got blocked
    )
    session.add(old_task)

    new_task = Task(
        board_id=board.id,
        assigned_agent_id=worker.id,
        title="Fresh inbox work",
        status="inbox",
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(old_task)
    await session.refresh(new_task)

    # Age the old task's blocked transition past the 15min grace window.
    from sqlmodel import update
    await session.exec(
        update(Task)
        .where(Task.id == old_task.id)
        .values(blocked_at=now - dt.timedelta(minutes=20))
    )
    await session.commit()

    return board, lead, lead_raw, worker, worker_raw, old_task, new_task


@pytest.mark.asyncio
async def test_unblock_race_requeues_old_task_and_poll_returns_new(
    client: AsyncClient, async_session,
):
    """Full race replay: claim new → unblock old → old task must be inbox
    (NOT a second in_progress) and poll must report working on the NEW task."""
    board, lead, lead_token, worker, worker_token, old_task, new_task = await _setup(async_session)

    # ── Step 1: grace window expired → worker claims the NEW inbox task ──
    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        poll1 = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
    assert poll1.status_code == 200
    body1 = poll1.json()
    assert body1["state"] == "new_task", body1
    assert body1["task"]["id"] == str(new_task.id)

    # Claim set the active-task lock on the worker.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        w = await s.get(Agent, worker.id)
        assert w.current_task_id == new_task.id

    # ── Step 2: worker ACKs the new task (inbox → in_progress) ──
    ack = await client.patch(
        f"/api/v1/agent/boards/{board.id}/tasks/{new_task.id}",
        json={"status": "in_progress"},
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Dispatch-Attempt-Id": body1["task"]["dispatch_attempt_id"],
        },
    )
    assert ack.status_code == 200, ack.text

    # ── Step 3: seconds later, the lead unblocks the OLD task ──
    unblock = await client.patch(
        f"/api/v1/agent/boards/{board.id}/tasks/{old_task.id}",
        json={"status": "in_progress"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert unblock.status_code == 200, unblock.text

    # ── Assert: no second in_progress — old task was requeued to inbox ──
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        old_refreshed = await s.get(Task, old_task.id)
        assert old_refreshed.status == "inbox", (
            f"old task must be requeued, not a second in_progress: {old_refreshed.status}"
        )
        assert old_refreshed.dispatched_at is None
        assert old_refreshed.ack_at is None

        new_refreshed = await s.get(Task, new_task.id)
        assert new_refreshed.status == "in_progress"

    # ── Assert: poll reports working on the NEW task, not the old one ──
    poll2 = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert poll2.status_code == 200
    body2 = poll2.json()
    assert body2["state"] == "working", body2
    assert body2["task_id"] == str(new_task.id), (
        f"poll surfaced the wrong task: {body2}"
    )


@pytest.mark.asyncio
async def test_poll_prefers_current_task_id_over_updated_at_ordering(
    client: AsyncClient, async_session,
):
    """Poll hardening in isolation: two in_progress tasks (data corruption
    from before the fix, or any other race) — poll must report the task the
    agent actually runs (current_task_id), not the freshest-updated one."""
    board = Board(name="Order Board", slug=f"order-{uuid.uuid4().hex[:8]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name="Sparky",
        agent_runtime="docker",
        agent_token_hash=token_hash,
        board_id=board.id,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    now = dt.datetime.now(tz=dt.timezone.utc)
    real_task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Actually running",
        status="in_progress",
        dispatched_at=now,
        ack_at=now,
    )
    shadow_task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Freshly touched shadow",
        status="in_progress",
        dispatched_at=now,
        ack_at=now,
    )
    async_session.add(real_task)
    async_session.add(shadow_task)
    await async_session.commit()
    await async_session.refresh(real_task)
    await async_session.refresh(shadow_task)

    # The shadow task is the freshest-updated; the agent runs real_task.
    from sqlmodel import update
    await async_session.exec(
        update(Task).where(Task.id == real_task.id).values(
            updated_at=now - dt.timedelta(minutes=30)
        )
    )
    await async_session.commit()

    agent.current_task_id = real_task.id
    async_session.add(agent)
    await async_session.commit()

    poll = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert poll.status_code == 200
    body = poll.json()
    assert body["state"] == "working", body
    assert body["task_id"] == str(real_task.id), (
        f"poll must prefer current_task_id over updated_at ordering: {body}"
    )
