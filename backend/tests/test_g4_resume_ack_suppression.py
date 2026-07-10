"""G4 (W2-A, Fix 3) — Tier-3 recovery resume must not re-arm the ACK
escalation ladder.

Tier-3 recovery (task_runner._run_tiered_recovery) resets
task.dispatched_at/ack_at then redispatches via auto_dispatch_task —
semantically a RESUME of the run the recovery is trying to save, NOT a
fresh dispatch. Without suppression, _check_dispatch_ack's ACK-timeout
ladder could re-arm on that reset and fire its OWN escalation Approval
concurrently with the recovery that caused it (double-escalation for one
root event).

Fix: after a Tier-3 reset, set a short Redis suppression key
(RedisKeys.dispatch_resume_suppress, TTL = agent's ack timeout + margin)
that _check_dispatch_ack checks and skips (log debug, no approval). A
genuinely-never-acked resume still escalates once the suppression window
elapses.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.redis_client import RedisKeys
from app.utils import utcnow


@asynccontextmanager
async def _patched_test_session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _make_fixtures(make_board, make_agent, make_task, *, status: str = "in_progress"):
    board = await make_board(name="G4 Board", slug=f"g4-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        name="Worker-G4", board_id=board.id, is_board_lead=False,
        role="developer", agent_runtime="docker",
    )
    task = await make_task(
        board_id=board.id, title="G4 resume task", status=status,
        assigned_agent_id=agent.id,
    )
    return board, agent, task


# ── Test 1: Tier-3 resume sets the suppression key ──────────────────────


@pytest.mark.asyncio
async def test_tier3_resume_sets_suppression_key(
    fake_redis, make_board, make_agent, make_task,
):
    """A successful Tier-3 resume must set RedisKeys.dispatch_resume_suppress
    with a positive TTL, so a concurrent _check_dispatch_ack tick doesn't
    re-arm the ACK ladder for the very run recovery is saving."""
    _board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task, status="in_progress",
    )

    from app.services.task_runner import task_runner

    restart_spy = MagicMock(return_value={"status": "restarted", "container": "mc-agent-test"})

    async def _fake_dispatch(task_id, board_id):
        async with _patched_test_session() as inner_session:
            from app.models.task import Task
            t = await inner_session.get(Task, task_id)
            t.dispatched_at = utcnow()
            inner_session.add(t)
            await inner_session.commit()

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.docker_agent_sync.restart_docker_agent_container", restart_spy), \
         patch("app.services.task_runner.auto_dispatch_task", AsyncMock(side_effect=_fake_dispatch)), \
         patch("app.services.task_runner.logger"), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        async with _patched_test_session() as session:
            result = await task_runner._run_tiered_recovery(session, task, agent)

    assert result is True

    key = RedisKeys.dispatch_resume_suppress(str(task.id))
    assert await fake_redis.get(key) is not None, "suppression key must be set after Tier-3 resume"
    ttl = await fake_redis.ttl(key)
    assert ttl > 0, f"suppression key must have a positive TTL, got {ttl}"
    # docker runtime has no AGENT_RUNTIME_ACK_TIMEOUTS override -> falls back
    # to _DEFAULT_ACK_TIMEOUT_MINUTES (5) + RESUME_SUPPRESS_MARGIN_MINUTES (5) = 10min = 600s
    assert ttl <= 600, f"suppression TTL should be bounded (ack timeout + margin), got {ttl}s"


# ── Test 2: suppressed task is skipped by _check_dispatch_ack ───────────


@pytest.mark.asyncio
async def test_check_dispatch_ack_skips_suppressed_task(
    fake_redis, make_board, make_agent, make_task,
):
    """A task with dispatched_at far enough in the past to normally trigger
    ACK-timeout escalation must be SKIPPED while the resume-suppression key
    is set — no approval created, no dispatch_attempt rotation."""
    from app.models.approval import Approval
    from sqlmodel import select

    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task, status="inbox",
    )

    # Dispatched well past docker's fallback ack timeout (5min default,
    # no AGENT_RUNTIME_ACK_TIMEOUTS entry for "docker") — would normally
    # escalate to an Approval.
    async with _patched_test_session() as session:
        from app.models.task import Task
        t = await session.get(Task, task.id)
        t.dispatched_at = utcnow() - timedelta(minutes=30)
        session.add(t)
        await session.commit()

    # Simulate an in-flight Tier-3 resume suppression.
    await fake_redis.set(RedisKeys.dispatch_resume_suppress(str(task.id)), "1", ex=600)

    from app.services.task_runner import task_runner

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_runner.logger"):
        async with _patched_test_session() as session:
            await task_runner._check_dispatch_ack(session)

    async with _patched_test_session() as session:
        approvals = (await session.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all()
    assert not approvals, (
        f"Suppressed task must NOT get an escalation Approval. Got: {len(approvals)}"
    )


# ── Test 3: sanity — without suppression, ACK timeout still escalates ───


@pytest.mark.asyncio
async def test_check_dispatch_ack_still_escalates_without_suppression(
    fake_redis, make_board, make_agent, make_task,
):
    """Regression guard: a genuinely-never-acked, non-suppressed dispatch
    must still escalate normally — the suppression key must not blanket-
    disable the ladder."""
    from app.models.approval import Approval
    from sqlmodel import select

    board, agent, task = await _make_fixtures(
        make_board, make_agent, make_task, status="inbox",
    )

    async with _patched_test_session() as session:
        from app.models.task import Task
        t = await session.get(Task, task.id)
        t.dispatched_at = utcnow() - timedelta(minutes=30)
        session.add(t)
        await session.commit()

    # No suppression key set this time.
    from app.services.task_runner import task_runner

    with patch("app.services.task_runner.get_redis", AsyncMock(return_value=fake_redis)), \
         patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_runner.logger"), \
         patch("app.services.task_runner.TaskRunnerService._create_dispatch_approval", new_callable=AsyncMock) as approval_spy:
        async with _patched_test_session() as session:
            await task_runner._check_dispatch_ack(session)

    assert approval_spy.await_count == 1, (
        "Without suppression, a stale never-acked dispatch must still escalate"
    )
