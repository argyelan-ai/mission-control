"""Bauplan Lauf 2 Teil 3 (analyse.md, 21.09.2026): the working->idle
heartbeat flank drains the agent's Redis task queue when a host agent's
turn ends.

Problem: `drain_agent_task_queue` today only fires on task completion
(task_lifecycle.py:1047/1412), but Hermes marks its task `done`/`review`
BEFORE its ACP turn actually finishes (it keeps writing a summary/posting
comments) — so a card Guard 3 (Teil 2) queued during that turn sits until
the 15-minute pending escalation fires. This heartbeat flank is the
missing trigger: `agent_heartbeat` (routers/agents.py) must notice the
status transition working -> idle (captured BEFORE any mutation, in
`_prev_status`) and, AFTER the commit, fire
`create_tracked_task(drain_agent_task_queue(str(agent.id)))`.

RED (21.09.2026): none of this exists yet — `drain_agent_task_queue` is
never called from the heartbeat handler, so all four tests fail because
the patched mock is never invoked (or, for the failure-tolerance test,
because there is nothing calling it that could raise).
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent_with_active_task(*, status: str):
    """Host agent with ONE in_progress task assigned — mirrors a real
    Hermes turn (task stays in_progress while the bridge is mid-turn), so
    the heartbeat self-heal in routers/agents.py keeps `status` following
    the payload for both 'working' and 'idle' instead of the Bug-18
    no-active-task coercion path."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id, name=f"HBDrain-{uuid.uuid4().hex[:6]}",
            slug=f"hbdrain-{uuid.uuid4().hex[:6]}",
        ))
        await s.commit()

        agent = Agent(
            id=agent_id,
            name=f"HermesLike-{uuid.uuid4().hex[:6]}",
            role="developer",
            board_id=board_id,
            agent_runtime="host",
            status=status,
            agent_token_hash=token_hash,
            scopes=["heartbeat", "tasks:read"],
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()

        task = Task(
            board_id=board_id,
            title="Turn-end drain probe",
            status="in_progress",
            assigned_agent_id=agent_id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(agent)

    return agent, raw_token


async def _settle():
    """Let a fire-and-forget create_tracked_task background task run."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_working_to_idle_drains_queue(client: AsyncClient):
    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle()

    drain.assert_called_once_with(str(agent.id))


@pytest.mark.asyncio
async def test_idle_to_idle_does_not_drain(client: AsyncClient):
    agent, token = await _make_agent_with_active_task(status="idle")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle()

    drain.assert_not_called()


@pytest.mark.asyncio
async def test_working_to_working_does_not_drain(client: AsyncClient):
    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "working"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle()

    drain.assert_not_called()


@pytest.mark.asyncio
async def test_idle_to_working_does_not_drain(client: AsyncClient):
    """Only the working->idle flank drains — the reverse edge (turn just
    STARTED) must never trigger a drain."""
    agent, token = await _make_agent_with_active_task(status="idle")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "working"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle()

    drain.assert_not_called()


@pytest.mark.asyncio
async def test_drain_failure_does_not_break_heartbeat(client: AsyncClient):
    """A drain that raises must never surface as a broken heartbeat — the
    background task swallows/logs it (create_tracked_task's own
    contract), the HTTP response stays 200."""
    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    with patch(
        "app.services.task_queue.drain_agent_task_queue",
        new_callable=AsyncMock,
        side_effect=RuntimeError("redis is on fire"),
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle()

    drain.assert_called_once_with(str(agent.id))


@pytest.mark.asyncio
async def test_switch_off_keeps_legacy_no_drain(client: AsyncClient, monkeypatch):
    """Schalter (Bauplan 'Schalter', shared by Teil 2-4): with
    host_turn_signal_enabled=False, the working->idle flank must NOT drain
    — the rollback path needs no code change beyond this flag.

    Runde 2 (Pruefbericht Punkt 4): monkeypatch.setattr instead of a hard
    `= True` in `finally` — restores the pre-test value, not an assumption."""
    from app.config import settings

    agent, token = await _make_agent_with_active_task(status="working")
    headers = {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(settings, "host_turn_signal_enabled", False)
    with patch(
        "app.services.task_queue.drain_agent_task_queue", new_callable=AsyncMock
    ) as drain:
        resp = await client.post(
            "/api/v1/agent/me/heartbeat", json={"status": "idle"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await _settle()

    drain.assert_not_called()
