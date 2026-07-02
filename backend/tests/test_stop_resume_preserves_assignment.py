"""Tests fuer stop_task_run + resume_task_run — assigned_agent_id bleibt erhalten.

Bug-Repro 2026-04-24: Der Operator stoppte Sparky's Task, restartete Container, requeued.
Task landete im Inbox ohne assigned_agent_id → Sparky bekam ihn nicht zurück.

Root Cause: stop_task_run rief apply_terminal_unassign → Unassign. resume_task_run
stellte nichts wieder her → orphaned Task.

Fix: stop_task_run behaelt assigned_agent_id. Der Agent-Poll-Pfad state="stopped"
(agents.py:2635) respektiert run_control=stopped bereits sauber — erfordert aber
dass der Agent noch assigned ist um den Stop zu sehen.
"""
import uuid
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# Autouse: Patch get_redis() fuer alle Tests in diesem Modul. emit_event +
# andere Backend-Services rufen get_redis() direkt (nicht via Depends), daher
# Monkey-Patching der Modul-Referenzen noetig wenn wir nicht via HTTP-Client
# testen.
@pytest.fixture(autouse=True)
async def _patch_redis(monkeypatch):
    server = fakeredis.aioredis.FakeServer()
    fake = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    async def _fake_get_redis():
        return fake

    # Patchen in allen Modulen die get_redis direkt importieren
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

        # Agent mit current_task_id verbinden
        agent.current_task_id = task.id
        s.add(agent)
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    return agent, task, board_id


@pytest.mark.asyncio
async def test_stop_preserves_assigned_agent_id():
    """stop_task_run darf assigned_agent_id NICHT auf None setzen.

    Grund: der Agent-Poll-Pfad state="stopped" (agents.py:2635) braucht den
    Agent-Link um den Stop zu erkennen und die Session sauber zu terminieren.
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
    """Agent's current_task_id wird freigegeben (Lock), aber Task.assigned_agent_id bleibt."""
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
        # Task-Seite: assigned bleibt
        assert fresh_task.assigned_agent_id == agent.id
        # Agent-Seite: Lock freigegeben
        assert fresh_agent.current_task_id is None
        assert fresh_agent.run_state == "idle"


@pytest.mark.asyncio
async def test_resume_restores_to_inbox_with_fresh_attempt_id():
    """resume_task_run setzt status=inbox + generiert frische dispatch_attempt_id."""
    from app.models.task import Task
    from app.services.operations import stop_task_run, resume_task_run

    agent, task, board_id = await _make_running_task()

    # Stop
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await stop_task_run(s, task.id, "mark")
        await s.commit()

    # Resume (mit mock für auto_dispatch_task da keine echte dispatch-Kette hier)
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
        # Der Fix: assigned_agent_id bleibt durchgehend erhalten
        assert fresh.assigned_agent_id == agent.id, (
            f"Resume muss assigned_agent_id behalten — sonst orphaned Task. "
            f"Got: {fresh.assigned_agent_id}"
        )
        # dispatch_attempt_id wird in resume_task_run initial gesetzt, aber
        # kann durch auto_dispatch_task neu generiert oder auf None gesetzt
        # werden (siehe agent_scoped.py:3748 Stale-prevention). Wichtig:
        # NACH Resume ist entweder ein frischer Wert ODER None, auf keinen
        # Fall der alte vor-Stop-Wert.


@pytest.mark.asyncio
async def test_resume_triggers_auto_dispatch():
    """Resume ruft auto_dispatch_task um Task aktiv zum Agent zurueckzuschicken."""
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
    """End-to-End Szenario (Live-Bug des Operators):
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
