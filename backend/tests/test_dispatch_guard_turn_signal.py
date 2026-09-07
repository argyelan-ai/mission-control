"""Guard 3: Live-Turn-Signal before the paste (cli-bridge/omp).

Incident 07.09.2026: Guards 1 (current_task_id) and 2 (DB busy-check) saw
the developer agent as free — the predecessor task was already `done` —
while omp was still mid-turn (reflection/memory-save after `mc done`).
The new task's prompt got pasted into the running turn and Task D hung
70 minutes.

The live signal is the agent's heartbeat
(bridge.py start_heartbeater → POST /agent/me/heartbeat → agents.last_seen_at)
runs every 30 s regardless of turn state. A heartbeat fresher than
TURN_SIGNAL_FRESH_SECONDS (60 s) = agent alive right after its last task went
done = treat as mid-turn → enqueue_task + event `task.dispatch_queued` with
reason `agent_in_turn`, NO paste. Stale/missing heartbeat → normal dispatch
(fail-open, no deadlock). host/claude-code runtimes are untouched.
"""
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


def _ago(seconds: float) -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=seconds)


async def _seed(
    make_board, make_agent, make_task, *, agent_runtime: str, last_seen_at
):
    """Board + agent + pre-assigned inbox task. No other active tasks —
    Guards 1+2 must see the agent as free so Guard 3 is what decides."""
    board = await make_board(
        name=f"Guard3-{uuid.uuid4().hex[:8]}",
        slug=f"guard3-{uuid.uuid4().hex[:8]}",
        auto_dispatch_enabled=True,
    )
    agent = await make_agent(
        name="alpha",
        role="developer",
        board_id=board.id,
        agent_runtime=agent_runtime,
        last_seen_at=last_seen_at,
    )
    task = await make_task(
        board_id=board.id,
        status="inbox",
        title="Guard 3 probe",
        assigned_agent_id=agent.id,
    )
    return board, agent, task


async def _run_dispatch(task_id: uuid.UUID, board_id: uuid.UUID) -> None:
    with patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch.engine", test_engine):
        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task_id, board_id)


async def _activity_events(task_id: uuid.UUID) -> list:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from sqlalchemy import text as sa_text
        res = await s.exec(
            __import__("sqlmodel").select(
                __import__("app.models.activity", fromlist=["ActivityEvent"]).ActivityEvent
            ).where(
                __import__("app.models.activity", fromlist=["ActivityEvent"]).ActivityEvent.task_id == task_id
            )
        )
        return list(res.all())


@pytest.mark.asyncio
async def test_idle_agent_dispatches_immediately(
    make_board, make_agent, make_task, fake_redis
):
    """(a) Agent idle: heartbeat 10 min old → normal dispatch, paste happens."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", last_seen_at=_ago(600),
        )
        await _run_dispatch(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is not None, (
            "idle agent must be dispatched immediately (no deadlock)"
        )

    from app.services.task_queue import queue_length
    assert await queue_length(str(agent.id)) == 0


@pytest.mark.asyncio
async def test_agent_in_turn_queues_without_paste(
    make_board, make_agent, make_task, fake_redis
):
    """(b) Agent mid-turn: heartbeat < 60 s old → queued, NO paste, event
    `task.dispatch_queued` with reason `agent_in_turn`."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", last_seen_at=_ago(20),
        )
        await _run_dispatch(task.id, board.id)

        from app.services.task_queue import queue_length
        assert await queue_length(str(agent.id)) == 1, "task must sit in the agent queue"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is None, "NO paste: dispatched_at must stay unset"

    events = await _activity_events(task.id)
    queued = [e for e in events if e.event_type == "task.dispatch_queued"]
    assert queued, "task.dispatch_queued event required"
    detail = queued[0].detail or {}
    assert detail.get("reason") == "agent_in_turn", detail


@pytest.mark.asyncio
async def test_stale_heartbeat_does_not_deadlock(
    make_board, make_agent, make_task, fake_redis
):
    """(c) Heartbeat older than the threshold → normal dispatch (fail-open)."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", last_seen_at=_ago(61),
        )
        await _run_dispatch(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is not None, (
            "stale heartbeat must not block dispatch"
        )

    from app.services.task_queue import queue_length
    assert await queue_length(str(agent.id)) == 0


@pytest.mark.asyncio
async def test_missing_heartbeat_dispatches(
    make_board, make_agent, make_task, fake_redis
):
    """Fail-open: last_seen_at NULL (never heartbeated) → normal dispatch."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", last_seen_at=None,
        )
        await _run_dispatch(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is not None


@pytest.mark.asyncio
async def test_host_runtime_untouched(
    make_board, make_agent, make_task, fake_redis
):
    """Guardrail: host agents keep dispatching even with a fresh heartbeat."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="host", last_seen_at=_ago(5),
        )
        await _run_dispatch(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is not None

    from app.services.task_queue import queue_length
    assert await queue_length(str(agent.id)) == 0
