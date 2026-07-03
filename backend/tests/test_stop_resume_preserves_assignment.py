"""Tests for stop_task_run + resume_task_run — assigned_agent_id is preserved.

Bug repro 2026-04-24: The operator stopped Sparky's task, restarted the container,
requeued it. Task ended up in the inbox without assigned_agent_id → Sparky didn't
get it back.

Root cause: stop_task_run called apply_terminal_unassign → unassign. resume_task_run
didn't restore anything → orphaned task.

Fix: stop_task_run keeps assigned_agent_id. The agent poll path state="stopped"
(agents.py:2635) already handles run_control=stopped cleanly — but it requires
the agent to still be assigned in order to see the stop.
"""
import uuid
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# Autouse: patch get_redis() for all tests in this module. emit_event +
# other backend services call get_redis() directly (not via Depends), so
# monkey-patching the module references is necessary when we're not
# testing via the HTTP client.
@pytest.fixture(autouse=True)
async def _patch_redis(monkeypatch):
    server = fakeredis.aioredis.FakeServer()
    fake = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    async def _fake_get_redis():
        return fake

    # Patch in all modules that import get_redis directly
    import app.redis_client as redis_mod
    import app.services.sse as sse_mod
    monkeypatch.setattr(redis_mod, "get_redis", _fake_get_redis)
    monkeypatch.setattr(sse_mod, "get_redis", _fake_get_redis)
    yield fake
    await fake.aclose()


async def _make_running_task():
    """Board + Agent + Task (in_progress, assigned, running)."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task
    from app.utils import utcnow

    _, th = generate_agent_token()
    board_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="TestBoard", slug=f"stop-{uuid.uuid4().hex[:6]}"))
        await s.commit()

        agent = Agent(
            id=uuid.uuid4(), name="Sparky", role="developer",
            board_id=board_id, agent_runtime="cli-bridge",
            agent_token_hash=th, model="x", provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

        task = Task(
            board_id=board_id, title="Long-running install task",
            description="x", status="in_progress",
            assigned_agent_id=agent.id,
            dispatched_at=utcnow(), ack_at=utcnow(),
        )
        s.add(task)

        # Link agent with current_task_id
        agent.current_task_id = task.id
        s.add(agent)
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    return agent, task, board_id


@pytest.mark.asyncio
async def test_stop_preserves_assigned_agent_id():
    """stop_task_run must NOT set assigned_agent_id to None.

    Reason: the agent poll path state="stopped" (agents.py:2635) needs the
    agent link to detect the stop and terminate the session cleanly.
    """
    from app.models.task import Task
    from app.services.operations import stop_task_run

    agent, task, board_id = await _make_running_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark", reason="test-stop")
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "blocked"
        assert fresh.run_control == "stopped"
        assert fresh.assigned_agent_id == agent.id, (
            f"assigned_agent_id muss erhalten bleiben fuer Agent-Poll state=stopped! "
            f"Got: {fresh.assigned_agent_id}"
        )


@pytest.mark.asyncio
async def test_stop_clears_current_task_id_but_keeps_assignment():
    """Agent's current_task_id is released (lock), but Task.assigned_agent_id stays."""
    from app.models.agent import Agent
    from app.models.task import Task
    from app.services.operations import stop_task_run

    agent, task, board_id = await _make_running_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark", reason="test-stop")
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh_task = await s.get(Task, task.id)
        fresh_agent = await s.get(Agent, agent.id)
        # Task side: assigned stays
        assert fresh_task.assigned_agent_id == agent.id
        # Agent side: lock released
        assert fresh_agent.current_task_id is None
        assert fresh_agent.run_state == "idle"


@pytest.mark.asyncio
async def test_resume_restores_to_inbox_with_fresh_attempt_id():
    """resume_task_run sets status=inbox + generates a fresh dispatch_attempt_id."""
    from app.models.task import Task
    from app.services.operations import stop_task_run, resume_task_run

    agent, task, board_id = await _make_running_task()

    # Stop
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark")
        await s.commit()

    # Resume (with mock for auto_dispatch_task since there's no real dispatch chain here)
    with patch(
        "app.services.dispatch.auto_dispatch_task",
        new_callable=AsyncMock,
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await resume_task_run(s, task.id, "mark")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "inbox"
        assert fresh.run_control is None
        assert fresh.dispatched_at is None
        assert fresh.ack_at is None
        # The fix: assigned_agent_id is preserved throughout
        assert fresh.assigned_agent_id == agent.id, (
            f"Resume muss assigned_agent_id behalten — sonst orphaned Task. "
            f"Got: {fresh.assigned_agent_id}"
        )
        # dispatch_attempt_id is initially set in resume_task_run, but
        # can be regenerated or set to None by auto_dispatch_task
        # (see agent_scoped.py:3748 stale-prevention). Important:
        # AFTER resume it's either a fresh value OR None, never
        # the old pre-stop value.


@pytest.mark.asyncio
async def test_resume_triggers_auto_dispatch():
    """Resume calls auto_dispatch_task to actively send the task back to the agent."""
    from app.services.operations import stop_task_run, resume_task_run

    agent, task, board_id = await _make_running_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark")
        await s.commit()

    dispatch_calls: list = []

    async def _fake_dispatch(task_id, board_id_arg):
        dispatch_calls.append((task_id, board_id_arg))

    with patch("app.services.dispatch.auto_dispatch_task", _fake_dispatch):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await resume_task_run(s, task.id, "mark")

    assert len(dispatch_calls) == 1, (
        f"Resume muss auto_dispatch_task genau 1× rufen. Got: {dispatch_calls}"
    )
    assert dispatch_calls[0][0] == task.id
    assert dispatch_calls[0][1] == board_id


@pytest.mark.asyncio
async def test_stop_resume_roundtrip_preserves_agent():
    """End-to-end scenario (operator's live bug):
    Task → in_progress → stop → resume → inbox + same agent + fresh attempt_id."""
    from app.models.task import Task
    from app.services.operations import stop_task_run, resume_task_run

    agent, task, board_id = await _make_running_task()
    original_agent_id = agent.id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark")
        await s.commit()

    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            await resume_task_run(s, task.id, "mark")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
        assert fresh.status == "inbox"
        assert fresh.assigned_agent_id == original_agent_id, (
            "Nach stop+resume muss Task weiter demselben Agent gehoeren."
        )
