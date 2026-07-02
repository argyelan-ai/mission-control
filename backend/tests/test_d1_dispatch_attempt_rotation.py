"""Tests fuer D-1: dispatch_attempt_id Self-Heal-Rotation in task_runner.

Hintergrund: Wenn der erste paste-and-submit eines Dispatch verloren geht
(false-negative paste-verify, Bug 16-style), bleibt poll.sh haengen ohne ACK
weil LAST_DISPATCHED_ATTEMPT_ID == aktuelle attempt_id. Backend Self-Heal:
nach ack_timeout/2 → dispatch_attempt_id rotieren → poll.sh sieht neue
attempt_id → re-paste.

Sparky-Live-Symptom 2026-05-14: Task 1c67428e haengt 2.7h ohne ACK, weil
poll.sh dachte attempt sei gesendet, aber LLM-Pane hat ihn nie gesehen.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@pytest.mark.asyncio
async def test_rotation_skipped_before_threshold(fake_redis, make_board, make_agent, make_task):
    """Dispatch <= ack_timeout/2 min alt → KEINE Rotation."""
    from app.services.task_runner import task_runner
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host",
scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    # ack_timeout for host = 5min, threshold = 2.5min, dispatch vor 1min
    one_min_ago = _now() - timedelta(minutes=1)
    original_attempt = str(uuid.uuid4())
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=one_min_ago,
        dispatch_attempt_id=original_attempt,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rotated = await task_runner._maybe_rotate_dispatch_attempt(
            s, await s.get(type(task), task.id), agent,
            minutes_since_dispatch=1.0, redis=fake_redis, ack_timeout=5.0,
        )
    assert rotated is False

    # Task attempt_id unchanged
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(type(task), task.id)
    assert fresh.dispatch_attempt_id == original_attempt


@pytest.mark.asyncio
async def test_rotation_happens_at_threshold(fake_redis, make_board, make_agent, make_task):
    """Dispatch >= ack_timeout/2 → Rotation, neue attempt_id, Redis-Marker, Event."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.models.activity import ActivityEvent
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel import select
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host",
scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    three_min_ago = _now() - timedelta(minutes=3)
    original_attempt = str(uuid.uuid4())
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=three_min_ago,
        dispatch_attempt_id=original_attempt,
    )

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            rotated = await task_runner._maybe_rotate_dispatch_attempt(
                s, await s.get(Task, task.id), agent,
                minutes_since_dispatch=3.0, redis=fake_redis, ack_timeout=5.0,
            )
        assert rotated is True

    # Task attempt_id changed
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.dispatch_attempt_id != original_attempt
    assert fresh.dispatch_attempt_id is not None

    # Redis dedup marker set
    assert await fake_redis.get(f"mc:task:{task.id}:attempt_rotated") == "1"

    # Activity event emitted
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        events = (await s.exec(
            select(ActivityEvent)
            .where(ActivityEvent.event_type == "task.dispatch_attempt_rotated")
            .where(ActivityEvent.task_id == task.id)
        )).all()
    assert len(events) == 1
    assert "Silent retry" in events[0].title


@pytest.mark.asyncio
async def test_rotation_dedup_via_redis(fake_redis, make_board, make_agent, make_task):
    """Wenn Redis-Marker existiert → KEINE zweite Rotation."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host",
scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    three_min_ago = _now() - timedelta(minutes=3)
    original_attempt = str(uuid.uuid4())
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=three_min_ago,
        dispatch_attempt_id=original_attempt,
    )

    # Marker schon gesetzt (vorherige Rotation)
    await fake_redis.set(f"mc:task:{task.id}:attempt_rotated", "1")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rotated = await task_runner._maybe_rotate_dispatch_attempt(
            s, await s.get(Task, task.id), agent,
            minutes_since_dispatch=3.0, redis=fake_redis, ack_timeout=5.0,
        )
    assert rotated is False

    # attempt_id unchanged
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.dispatch_attempt_id == original_attempt


@pytest.mark.asyncio
async def test_handle_ack_timeout_uses_rotation_first(fake_redis, make_board, make_agent, make_task):
    """Integration: _handle_ack_timeout ruft Rotation auf vor full timeout."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.models.approval import Approval
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel import select
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host",
scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    # host ack_timeout = 5min, threshold = 2.5min. dispatch vor 3min → rotation
    three_min_ago = _now() - timedelta(minutes=3)
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=three_min_ago,
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await task_runner._handle_ack_timeout(
                s, await s.get(Task, task.id), agent, _now(), fake_redis,
            )

    # Rotation Marker im Redis (Rotation hat gegriffen, nicht Escalation)
    assert await fake_redis.get(f"mc:task:{task.id}:attempt_rotated") == "1"
    # KEINE Approval (Rotation hat early-returned)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 0


@pytest.mark.asyncio
async def test_handle_ack_timeout_escalates_after_full_timeout(fake_redis, make_board, make_agent, make_task):
    """Nach full ack_timeout → Approval (selbst wenn Rotation schon lief)."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.models.approval import Approval
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel import select
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host",
scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    # dispatch vor 10min — weit ueber ack_timeout=5min
    ten_min_ago = _now() - timedelta(minutes=10)
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=ten_min_ago,
        dispatch_attempt_id=str(uuid.uuid4()),
    )
    # Rotation schon gelaufen — markiert via Redis
    await fake_redis.set(f"mc:task:{task.id}:attempt_rotated", "1")

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await task_runner._handle_ack_timeout(
                s, await s.get(Task, task.id), agent, _now(), fake_redis,
            )

    # Approval entstanden
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(
                Approval.task_id == task.id,
                Approval.action_type == "dispatch_escalation",
            )
        )).all()
    assert len(approvals) == 1
