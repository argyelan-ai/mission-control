"""Guard 3: Live-Turn-Signal before the paste (cli-bridge/omp).

Incident 07.09.2026: Guards 1 (current_task_id) and 2 (DB busy-check) saw
the developer agent as free — the predecessor task was already `done` —
while omp was still mid-turn (reflection/memory-save after `mc done`).
The new task's prompt got pasted into the running turn and Task D hung
70 minutes.

The TURN signal is agent.status == "working" (PR #452 review): the bridge
heartbeater (bridge.py start_heartbeater → POST /agent/me/heartbeat →
routers/agents.py agent_heartbeat) derives it from the task-lock and
self-heals it against the task table — true only while a turn actually
runs. last_seen_at is merely a liveness beat every 30 s (idle agents have
a fresh one too), so it only gates staleness: a "working" agent whose
heartbeat is older than TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS (90 s) has
a dead bridge → fail-open dispatch, no deadlock. host/claude-code
runtimes are untouched.
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
    make_board, make_agent, make_task, *, agent_runtime: str,
    status: str, last_seen_at,
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
        status=status,
        last_seen_at=last_seen_at,
        # Guard 3 is about the turn-signal check, not git workspace setup —
        # the probe task below is ad-hoc (no project_id), and a
        # git-requiring agent would otherwise make dispatch actually try a
        # real GitHub clone (repo_registry.resolve_adhoc_repo_target, task
        # af914128), which has nothing to do with what this test verifies.
        requires_git_workflow=False,
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
    """(a) Agent idle with a fresh heartbeat (a real idle bridge heartbeats
    every 30 s) → normal dispatch, paste happens. last_seen_at freshness
    alone must NEVER queue — that was the PR #452 bug."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", status="idle", last_seen_at=_ago(15),
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
    """(b) Agent mid-turn: status=working + heartbeat < 90 s old → queued,
    NO paste, event `task.dispatch_queued` with reason `agent_in_turn`."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", status="working", last_seen_at=_ago(20),
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
    """(c) status=working but heartbeat older than the staleness gate →
    normal dispatch (fail-open, no deadlock)."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", status="working", last_seen_at=_ago(600),
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
    """Fail-open: status=working but last_seen_at NULL (never heartbeated)
    → normal dispatch."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="cli-bridge", status="working", last_seen_at=None,
        )
        await _run_dispatch(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is not None


@pytest.mark.asyncio
async def test_host_runtime_in_turn_queues(
    make_board, make_agent, make_task, fake_redis
):
    """Bauplan Lauf 2 Teil 2 (21.09.2026): Guard 3's runtime gate
    (`agent_runtime == "cli-bridge"`) drops — the turn signal itself
    (status=="working" + fresh heartbeat) is what decides, for ANY
    poll-based runtime including host. Was `test_host_runtime_untouched`,
    which asserted the opposite (host dispatches even in-turn) — that was
    the gap: 60/80 Hand-Starts (Nachpruefung N.2/N.5) trace back to a host
    agent's turn being invisible to Guard 3."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="host", status="working", last_seen_at=_ago(5),
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
async def test_host_stale_heartbeat_still_dispatches(
    make_board, make_agent, make_task, fake_redis
):
    """Sabotage probe for Teil 2: a host agent reporting status=working
    with a STALE heartbeat (dead bridge) must fail-open and dispatch
    normally — the same fail-open rule cli-bridge already has, now shared
    since the gate is runtime-free."""
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="host", status="working", last_seen_at=_ago(200),
        )
        await _run_dispatch(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is not None, (
            "stale heartbeat must not block dispatch (fail-open)"
        )

    from app.services.task_queue import queue_length
    assert await queue_length(str(agent.id)) == 0


@pytest.mark.asyncio
async def test_host_turn_signal_switch_off_keeps_legacy_dispatch(
    make_board, make_agent, make_task, fake_redis, monkeypatch
):
    """Schalter (Bauplan 'Schalter'): `settings.host_turn_signal_enabled`
    default True gates Teil 2-4 together. Switched OFF, a host agent
    in-turn must dispatch exactly like today (pre-fix) — the rollback
    path needs no code change, only this flag + a backend restart.

    Runde 2 (Pruefbericht Punkt 4): monkeypatch.setattr instead of a hard
    `= True` in `finally` — monkeypatch restores whatever value was there
    BEFORE this test ran, not a hardcoded assumption about the default."""
    from app.config import settings

    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        board, agent, task = await _seed(
            make_board, make_agent, make_task,
            agent_runtime="host", status="working", last_seen_at=_ago(5),
        )
        monkeypatch.setattr(settings, "host_turn_signal_enabled", False)
        await _run_dispatch(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        refreshed = await s.get(Task, task.id)
        assert refreshed.dispatched_at is not None, (
            "switch off must restore legacy (host dispatches even in-turn)"
        )

    from app.services.task_queue import queue_length
    assert await queue_length(str(agent.id)) == 0


def test_host_turn_signal_enabled_defaults_true():
    """Schalter default is ON — a default-OFF ships nothing (Bauplan)."""
    from app.config import settings

    assert settings.host_turn_signal_enabled is True
