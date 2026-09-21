"""Tests for D-1: dispatch_attempt_id self-heal rotation in task_runner.

Background: If the first paste-and-submit of a dispatch gets lost
(false-negative paste-verify, Bug 16-style), poll.sh hangs without an ACK
because LAST_DISPATCHED_ATTEMPT_ID == current attempt_id. Backend self-heal:
after ack_timeout/2 → rotate dispatch_attempt_id → poll.sh sees the new
attempt_id → re-paste.

Sparky live symptom 2026-05-14: task 1c67428e hangs 2.7h without ACK, because
poll.sh thought the attempt was sent, but the LLM pane never saw it.
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
    """Dispatch <= ack_timeout/2 min old → NO rotation."""
    from app.services.task_runner import task_runner
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host",
scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    # ack_timeout for host = 5min, threshold = 2.5min, dispatch 1min ago
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
    """Dispatch >= ack_timeout/2 → rotation, new attempt_id, Redis marker, event."""
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
    """If the Redis marker exists → NO second rotation."""
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

    # Marker already set (previous rotation)
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
    """Integration: _handle_ack_timeout calls rotation before full timeout."""
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

    # host ack_timeout = 5min, threshold = 2.5min. dispatch 3min ago → rotation
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

    # Rotation marker in Redis (rotation kicked in, not escalation)
    assert await fake_redis.get(f"mc:task:{task.id}:attempt_rotated") == "1"
    # NO approval (rotation returned early)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert len(approvals) == 0


@pytest.mark.asyncio
async def test_handle_ack_timeout_escalates_after_full_timeout(fake_redis, make_board, make_agent, make_task):
    """After full ack_timeout → approval (even if rotation already ran)."""
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

    # dispatch 10min ago — well beyond ack_timeout=5min
    ten_min_ago = _now() - timedelta(minutes=10)
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=ten_min_ago,
        dispatch_attempt_id=str(uuid.uuid4()),
    )
    # Rotation already ran — marked via Redis
    await fake_redis.set(f"mc:task:{task.id}:attempt_rotated", "1")

    from app.config import settings

    # Approval-Pfad = Schalter aus (Lauf 4)
    with patch.object(settings, "notice_only_escalations_enabled", False), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await task_runner._handle_ack_timeout(
                s, await s.get(Task, task.id), agent, _now(), fake_redis,
            )

    # Approval created
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(
                Approval.task_id == task.id,
                Approval.action_type == "dispatch_escalation",
            )
        )).all()
    assert len(approvals) == 1


# ── W-busy (#25efd77c): heal claimed while the agent's pty is busy ────────
#
# Incident (activity_events 14.09.2026, cards c0fb45c1/fd4ac7c6): a heal
# rotated dispatch_attempt_id at 05:32 while poll.sh's tmux pane was mid a
# DIFFERENT running turn ("Zug"). poll.sh's dispatch paste is fail-open
# (docker/shared/poll.sh paste_and_submit) — it pastes into the busy pane
# anyway, and the paste gets silently absorbed into that unrelated turn.
# The agent never ACKs. Pre-fix, `rotated_key`'s TTL was always the FULL
# ack_timeout window ("one heal per card per window"), so the card then
# had no further self-heal until the full ack_timeout escalated to a
# human — it stayed unacked for 3h until a manual poll.sh restart.
#
# Fix: `_maybe_rotate_dispatch_attempt` now checks agent.status at heal
# time. Busy (status=="working") → short BUSY_HEAL_RETRY_TTL_SEC lock, so
# the next task-runner tick can retry. Idle → unchanged full-window lock
# (the normal case: nothing already running, the paste has every chance
# to land, so re-rotating would risk a double dispatch — that path must
# stay untouched).


@pytest.mark.asyncio
async def test_busy_agent_heal_gets_short_retry_ttl(fake_redis, make_board, make_agent, make_task):
    """Heal claimed while agent.status=='working' → short retry TTL on
    rotated_key, not the full ack_timeout window."""
    from app.services.task_runner import task_runner, BUSY_HEAL_RETRY_TTL_SEC
    from app.models.task import Task
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host", status="working",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    three_min_ago = _now() - timedelta(minutes=3)
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=three_min_ago,
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            rotated = await task_runner._maybe_rotate_dispatch_attempt(
                s, await s.get(Task, task.id), agent,
                minutes_since_dispatch=3.0, redis=fake_redis, ack_timeout=5.0,
            )
    assert rotated is True

    ttl = await fake_redis.ttl(f"mc:task:{task.id}:attempt_rotated")
    assert 0 < ttl <= BUSY_HEAL_RETRY_TTL_SEC
    # Not the full ack_timeout window (5min = 300s) — that's the whole bug.
    assert ttl < 5 * 60


@pytest.mark.asyncio
async def test_idle_agent_heal_keeps_full_window_lock(fake_redis, make_board, make_agent, make_task):
    """Gegenrichtung: an idle agent's heal (the normal case) must keep the
    FULL ack_timeout lock — relaxing it would risk a double dispatch for a
    paste that had every chance to land."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    board = await make_board()
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host", status="idle",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )

    three_min_ago = _now() - timedelta(minutes=3)
    task = await make_task(
        board_id=board.id, status="inbox",
        assigned_agent_id=agent.id, dispatched_at=three_min_ago,
        dispatch_attempt_id=str(uuid.uuid4()),
    )

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            rotated = await task_runner._maybe_rotate_dispatch_attempt(
                s, await s.get(Task, task.id), agent,
                minutes_since_dispatch=3.0, redis=fake_redis, ack_timeout=5.0,
            )
    assert rotated is True

    ttl = await fake_redis.ttl(f"mc:task:{task.id}:attempt_rotated")
    # Full ack_timeout window (5min = 300s), unchanged from pre-fix behaviour.
    assert ttl > 4 * 60


@pytest.mark.asyncio
async def test_busy_heal_retries_again_once_short_ttl_elapses(fake_redis, make_board, make_agent, make_task):
    """Reproduction + fix: a busy-agent heal rotates once, and — once its
    short TTL has elapsed (simulated here by clearing the redis keys, exactly
    what TTL expiry does) — a SECOND, DIFFERENT attempt_id is produced while
    the agent is still busy/unacked. `try_claim_heal`'s cross-healer 90s lock
    (mc:heal:<task_id>) is cleared alongside `rotated_key` between calls —
    both would naturally have expired for real by the time BUSY_HEAL_RETRY_TTL_SEC
    (120s) elapses in production (HEAL_DEDUP_TTL=90s < 120s), so clearing both
    here models real elapsed time, not a shortcut around the mechanism under
    test.

    Sabotage: with BUSY_HEAL_RETRY_TTL_SEC patched to the old
    always-full-window value, the second call stays blocked — reproducing
    the incident (card stuck on the swallowed attempt_id for the rest of the
    ack_timeout window despite still being busy/unacked)."""
    from app.services.task_runner import task_runner
    from app.models.task import Task
    from app.redis_client import RedisKeys
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async def _clear_dedup_keys(task_id):
        await fake_redis.delete(f"mc:task:{task_id}:attempt_rotated")
        await fake_redis.delete(RedisKeys.task_heal_claim(str(task_id)))

    async def _clear_heal_claim_only(task_id):
        # Simulates only the cross-healer 90s round-claim (try_claim_heal)
        # having expired for real — NOT the rotated_key itself, so the
        # sabotaged full-window TTL on rotated_key is still the thing under
        # test for the second call.
        await fake_redis.delete(RedisKeys.task_heal_claim(str(task_id)))

    async def _make_busy_task(agent):
        three_min_ago = _now() - timedelta(minutes=3)
        return await make_task(
            board_id=agent.board_id, status="inbox",
            assigned_agent_id=agent.id, dispatched_at=three_min_ago,
            dispatch_attempt_id=str(uuid.uuid4()),
        )

    async def _rotate(task, agent, minutes_since_dispatch):
        with patch("app.services.activity.broadcast", new_callable=AsyncMock):
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                result = await task_runner._maybe_rotate_dispatch_attempt(
                    s, await s.get(Task, task.id), agent,
                    minutes_since_dispatch=minutes_since_dispatch,
                    redis=fake_redis, ack_timeout=5.0,
                )
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            fresh = await s.get(Task, task.id)
        return result, fresh.dispatch_attempt_id

    board = await make_board()

    # ── Fixed behaviour: second heal gets through ──
    agent = await make_agent(
        name="Sparky", board_id=board.id, agent_runtime="host", status="working",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task = await _make_busy_task(agent)
    first_rotated, first_attempt = await _rotate(task, agent, 3.0)
    assert first_rotated is True

    await _clear_dedup_keys(task.id)  # simulates the short TTL elapsing
    second_rotated, second_attempt = await _rotate(task, agent, 4.0)
    assert second_rotated is True
    assert second_attempt != first_attempt, (
        "Card stayed on the swallowed attempt_id — busy heal did not retry"
    )

    # ── Sabotage: pre-fix constant (always full-window TTL) — second heal
    #    must stay blocked, reproducing the incident. ──
    agent2 = await make_agent(
        name="Sparky2", board_id=board.id, agent_runtime="host", status="working",
        scopes=["tasks:read", "tasks:write", "heartbeat"],
    )
    task2 = await _make_busy_task(agent2)
    with patch("app.services.task_runner.BUSY_HEAL_RETRY_TTL_SEC", 5 * 60):
        rotated_1, attempt_1 = await _rotate(task2, agent2, 3.0)
        assert rotated_1 is True
        # Only the cross-healer round-claim expires here, NOT rotated_key —
        # with the sabotaged full-window TTL, rotated_key is still set.
        await _clear_heal_claim_only(task2.id)
        rotated_2, attempt_2 = await _rotate(task2, agent2, 4.0)
    assert rotated_2 is False, (
        "Sabotage failed to reproduce the incident: with the old constant "
        "full-window TTL, a second heal should stay locked out"
    )
    assert attempt_2 == attempt_1
