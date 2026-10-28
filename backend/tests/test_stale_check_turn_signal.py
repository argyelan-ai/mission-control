"""The stale check never "recovers" an agent that is demonstrably mid-turn.

Live 27.10.2026 21:31 and 28.10.2026 15:56: a local-model agent thought for
8+ minutes per turn without posting a comment. After the 60-min idle
threshold the stale check ran tiered recovery ("Stale > 60min") although the
same agent was heartbeating ``working`` on this very card (dispatch had
just queued another card behind it: ``reason: agent_in_turn``,
``heartbeat_age_seconds: 23``). Recovery re-prepared the workspace mid-run,
posted a "you stopped here" recap and ended in a false "Auto-Recovery
fehlgeschlagen" alarm.

Rule: the turn signal dispatch Guard 3 already trusts (agent.status ==
"working" + heartbeat younger than TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS)
counts as activity on the card the agent works on (agent.current_task_id).
A stale heartbeat, or a turn on a different card, still recovers.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup(*, status: str, seen_ago_s: float | None, on_this_card: bool = True,
                 silent_hours: float = 3):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.utils import utcnow

    now = utcnow()
    board = Board(id=uuid.uuid4(), name="B", slug=f"ts-{uuid.uuid4().hex[:8]}")
    _raw, token_hash = generate_agent_token()
    agent_id = uuid.uuid4()
    task = Task(
        id=uuid.uuid4(), board_id=board.id, title="Long thinking run",
        status="in_progress", assigned_agent_id=agent_id,
        started_at=now - timedelta(hours=silent_hours),
        updated_at=now - timedelta(hours=silent_hours),
        ack_at=now - timedelta(hours=silent_hours),
    )
    other = Task(
        id=uuid.uuid4(), board_id=board.id, title="Other card",
        status="inbox", assigned_agent_id=agent_id,
    )
    agent = Agent(
        id=agent_id, name="Worker", board_id=board.id, agent_token_hash=token_hash,
        is_board_lead=False, role=None,
        scopes=["tasks:read", "tasks:write", "tasks:create"],
        status=status,
        current_task_id=task.id if on_this_card else other.id,
        last_seen_at=(now - timedelta(seconds=seen_ago_s)) if seen_ago_s is not None else None,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(board)
        s.add(agent)
        s.add(task)
        s.add(other)
        await s.commit()
    return task


async def _run_stale_check() -> AsyncMock:
    from app.services.task_runner import TaskRunnerService

    runner = TaskRunnerService()
    recovery = AsyncMock(return_value=True)
    runner._run_tiered_recovery = recovery  # type: ignore[method-assign]
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.emit_event", new_callable=AsyncMock):
            await runner._check_stale_in_progress(s)
    return recovery


@pytest.mark.asyncio
async def test_working_agent_with_fresh_heartbeat_is_not_recovered(fake_redis):
    """The live case: 3 h without a comment, but heartbeat 23 s old and
    status working on this card -> no recovery, no false alarm."""
    task = await _setup(status="working", seen_ago_s=23)
    recovery = await _run_stale_check()
    called_for = [c.args[1].id for c in recovery.await_args_list]
    assert task.id not in called_for, (
        "stale check ran tiered recovery on a card whose agent is mid-turn "
        "(status=working, heartbeat 23 s old)"
    )


@pytest.mark.asyncio
async def test_stale_heartbeat_still_recovers(fake_redis):
    """Counter-probe: the bridge stopped heartbeating (10 min) -> a real
    stall, recovery runs as before."""
    task = await _setup(status="working", seen_ago_s=600)
    recovery = await _run_stale_check()
    assert task.id in [c.args[1].id for c in recovery.await_args_list]


@pytest.mark.asyncio
async def test_idle_agent_with_fresh_heartbeat_still_recovers(fake_redis):
    """Counter-probe: alive but NOT in a turn (status idle) -> recovery."""
    task = await _setup(status="idle", seen_ago_s=10)
    recovery = await _run_stale_check()
    assert task.id in [c.args[1].id for c in recovery.await_args_list]


@pytest.mark.asyncio
async def test_turn_on_another_card_does_not_shield_this_one(fake_redis):
    """Counter-probe: the agent is mid-turn on a DIFFERENT card -> this
    card's silence is still a stall."""
    task = await _setup(status="working", seen_ago_s=10, on_this_card=False)
    recovery = await _run_stale_check()
    assert task.id in [c.args[1].id for c in recovery.await_args_list]


@pytest.mark.asyncio
async def test_turn_signal_shield_has_an_upper_bound(fake_redis):
    """Counter-probe: "working" only proves the heartbeat thread lives. After
    TURN_SIGNAL_SHIELD_FACTOR x the idle threshold (4 h) with no real
    activity, recovery runs even with a fresh heartbeat."""
    from app.services.task_runner import TURN_SIGNAL_SHIELD_FACTOR

    assert TURN_SIGNAL_SHIELD_FACTOR == 4
    task = await _setup(status="working", seen_ago_s=10, silent_hours=5)
    recovery = await _run_stale_check()
    assert task.id in [c.args[1].id for c in recovery.await_args_list]


@pytest.mark.asyncio
async def test_kill_switch_off_restores_old_behaviour(fake_redis, monkeypatch):
    """settings.host_turn_signal_enabled=False (the existing rollback switch
    of the turn signal) -> the old comment-only clock."""
    from app.config import settings

    monkeypatch.setattr(settings, "host_turn_signal_enabled", False)
    task = await _setup(status="working", seen_ago_s=23)
    recovery = await _run_stale_check()
    assert task.id in [c.args[1].id for c in recovery.await_args_list]
