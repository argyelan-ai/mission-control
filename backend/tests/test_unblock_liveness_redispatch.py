"""Tests for Workstream W2-B2: liveness- and occupancy-aware unblock.

Audit finding G3: the lead/operator unblock path (blocked→in_progress) only
ever posted an `unblock_notify` TaskComment — no liveness check, no
redispatch. If the blocked agent's process had died in the meantime, nobody
read the comment and the task silently stalled until the 15-45min
stale-recovery ladder caught it.

Fix (`resolve_unblock_action` + `redispatch_unblocked_task` in
task_lifecycle.py, wired into both agent_task_status.py's agent-scoped PATCH
and tasks.py's operator PATCH):
  - assigned agent ALIVE (last_seen_at fresh) + idle → comment only (today's
    behavior), cooldown-gated.
  - assigned agent ALIVE but occupied with a different in_progress task →
    comment only, no interrupt.
  - assigned agent DEAD/stale (last_seen_at NULL or beyond the wrapper
    liveness floor) → dispatched_at/ack_at reset + auto_dispatch_task
    re-dispatch (mocked in tests), no comment (redispatch carries recovery
    context instead).
"""
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


async def _setup(
    session: AsyncSession,
    *,
    target_last_seen: dt.datetime | None,
    target_current_task_id: uuid.UUID | None = None,
    target_heartbeat_interval: str = "5m",
):
    board = Board(name="Unblock Board", slug=f"unblock-{uuid.uuid4().hex[:8]}", blocker_triage_minutes=0)
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

    target_raw, target_hash = generate_agent_token()
    target = Agent(
        name="Sparky",
        role="developer",
        board_id=board.id,
        agent_token_hash=target_hash,
        is_board_lead=False,
        scopes=["tasks:read", "tasks:write"],
        last_seen_at=target_last_seen,
        heartbeat_config={"interval": target_heartbeat_interval},
    )
    session.add(target)
    await session.commit()
    await session.refresh(lead)
    await session.refresh(target)

    if target_current_task_id is not None:
        target.current_task_id = target_current_task_id
        session.add(target)
        await session.commit()

    task = Task(
        board_id=board.id,
        assigned_agent_id=target.id,
        title="Blocked probe",
        status="blocked",
        dispatched_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
        ack_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=30),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    return board, lead, target, lead_raw, task


@pytest.mark.asyncio
async def test_unblock_with_stale_agent_resets_dispatch_and_redispatches(client: AsyncClient, async_session):
    """Assigned agent's last_seen_at is way past the liveness floor (dead) →
    dispatched_at/ack_at reset to None + auto_dispatch_task invoked."""
    stale_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=stale_seen)

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch, patch("app.utils.create_tracked_task") as mock_create_tracked:
        # create_tracked_task just needs to run the coroutine so the mock
        # dispatch call is actually observed.
        def _run_now(coro, name=None):
            import asyncio
            return asyncio.ensure_future(coro)
        mock_create_tracked.side_effect = _run_now

        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text
        import asyncio
        await asyncio.sleep(0)  # let the tracked task run

    mock_dispatch.assert_called_once()
    called_task_id, called_board_id = mock_dispatch.call_args[0]
    assert called_task_id == task.id
    assert called_board_id == board.id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert not any(c.comment_type == "unblock_notify" for c in comments), (
            "dead-agent redispatch must not also post the unread comment"
        )


@pytest.mark.asyncio
async def test_unblock_with_fresh_idle_agent_posts_comment_only(client: AsyncClient, async_session):
    """Assigned agent's last_seen_at is fresh (alive, idle) → comment-only
    path, no redispatch/no dispatched_at reset."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)
    original_dispatched_at = task.dispatched_at

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text

    mock_dispatch.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        # dispatch handshake untouched — this is not a redispatch.
        assert refreshed.dispatched_at is not None
        assert refreshed.dispatched_at.replace(tzinfo=None) == original_dispatched_at.replace(tzinfo=None)

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert any(c.comment_type == "unblock_notify" for c in comments)


@pytest.mark.asyncio
async def test_unblock_respects_recovery_comment_cooldown(client: AsyncClient, async_session, fake_redis):
    """Fresh/alive agent path is still gated by the shared recovery-comment
    cooldown (G6) — a second unblock notify within the TTL is skipped."""
    from app.redis_client import try_claim_recovery_comment_cooldown

    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)

    # Pre-claim the cooldown for this task, simulating another mechanism
    # (Tier-3 recap etc.) having already fired.
    claimed = await try_claim_recovery_comment_cooldown(fake_redis, str(task.id))
    assert claimed is True

    resp = await client.patch(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
        json={"status": "in_progress"},
        headers={"Authorization": f"Bearer {lead_token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert not any(c.comment_type == "unblock_notify" for c in comments), (
            "cooldown already claimed by another mechanism — unblock_notify must be skipped"
        )


@pytest.mark.asyncio
async def test_unblock_with_busy_agent_requeues_without_interrupt(client: AsyncClient, async_session):
    """Review fix B-2: assigned agent is alive but occupied with a DIFFERENT
    in_progress task → the unblocked task must NOT stay in_progress (two
    in_progress tasks corrupt poll's active-task resolution). It goes back
    to inbox with dispatched_at/ack_at reset, so the normal claim flow
    re-delivers it after the current work. The other task is untouched — no
    interrupt, no immediate redispatch."""
    fresh_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=fresh_seen)

    # Give the target agent a different, currently in_progress task.
    other_task = Task(
        board_id=board.id,
        assigned_agent_id=target.id,
        title="Other active work",
        status="in_progress",
    )
    async_session.add(other_task)
    await async_session.commit()
    await async_session.refresh(other_task)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Agent, target.id)
        t.current_task_id = other_task.id
        s.add(t)
        await s.commit()

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text

    mock_dispatch.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # The other task must be untouched — no interrupt.
        other = await s.get(Task, other_task.id)
        assert other.status == "in_progress"
        busy_agent = await s.get(Agent, target.id)
        assert busy_agent.current_task_id == other_task.id, "active-task lock untouched"

        refreshed = await s.get(Task, task.id)
        assert refreshed.status == "inbox", (
            "unblocked task must be requeued to inbox, not left as a second in_progress"
        )
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None


@pytest.mark.asyncio
async def test_redispatch_clears_stale_current_task_pointer(client: AsyncClient, async_session):
    """Review fix B-3: when the dead-agent redispatch path fires and the
    agent's current_task_id still points at the task being redispatched,
    the pointer is cleared before auto_dispatch_task — nothing may read a
    stale active-task lock in the re-dispatch window."""
    stale_seen = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=2)
    board, lead, target, lead_token, task = await _setup(async_session, target_last_seen=stale_seen)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Agent, target.id)
        t.current_task_id = task.id
        s.add(t)
        await s.commit()

    with patch(
        "app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock
    ), patch("app.utils.create_tracked_task"):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {lead_token}"},
        )
        assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dead_agent = await s.get(Agent, target.id)
        assert dead_agent.current_task_id is None, (
            "stale current_task_id must be cleared before the re-dispatch"
        )
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is None
        assert refreshed.ack_at is None
