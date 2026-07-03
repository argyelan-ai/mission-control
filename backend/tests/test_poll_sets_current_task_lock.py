"""Tests for bug fix 2026-04-25: agent_poll sets the current_task_id lock.

Live bug for Boss on 2026-04-25: Boss received a task via push dispatch
(poll.sh fire-and-forget to claude), agent_poll set ack_at but NOT
agent.current_task_id. Boss immediately tried `mc delegate` (before its
first comment) → 409 "Kein aktiver Task". 6+ minute stewing loop until
Boss eventually got current_task_id via comment auto-ACK.

PR #103 fixed the PATCH-ACK path. PR ?? fixed the comment auto-ACK path.
Here's the third and final push-dispatch path: agent_poll.
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from tests.conftest import test_engine


async def _make_scenario(*, is_board_lead: bool, task_status: str = "in_progress"):
    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    now = dt.datetime.now(tz=dt.timezone.utc)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:6]}"))
        s.add(Agent(
            id=agent_id,
            name=f"TestAgent-{uuid.uuid4().hex[:4]}",
            agent_runtime="host",
            agent_token_hash=token_hash,
            board_id=board_id,
            is_board_lead=is_board_lead,
            current_task_id=None,
            scopes=["heartbeat", "tasks:read", "tasks:write"],
        ))
        s.add(Task(
            id=task_id,
            board_id=board_id,
            title="Push-Dispatch Test Task",
            status=task_status,
            assigned_agent_id=agent_id,
            dispatched_at=now,
            ack_at=None,
        ))
        await s.commit()

    return raw_token, board_id, agent_id, task_id


@pytest.mark.asyncio
async def test_agent_poll_sets_current_task_id_for_board_lead(
    client: AsyncClient, fake_redis,
):
    """Push dispatch to board lead: poll → current_task_id gets set.

    Prevents mc delegate / mc help-request / mc clarification from
    responding with 409 'Kein aktiver Task' even though a task is active.
    """
    raw_token, board_id, agent_id, task_id = await _make_scenario(is_board_lead=True)

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "new_task", body

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        assert agent.current_task_id == task_id, (
            f"Bug nicht gefixt: agent_poll setzt current_task_id nicht. "
            f"Got current_task_id={agent.current_task_id}, expected={task_id}. "
            f"Folge: nachfolgende mc delegate / mc help wuerden 409 zurueckgeben."
        )


@pytest.mark.asyncio
async def test_agent_poll_skips_current_task_id_for_worker_subagent_mode(
    client: AsyncClient, fake_redis, monkeypatch,
):
    """Subagent mode: workers don't get a lock — parallel sessions.

    Same skip condition as comment auto-ACK and PATCH-ACK.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", True)

    raw_token, board_id, agent_id, task_id = await _make_scenario(is_board_lead=False)

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        assert agent.current_task_id is None, (
            f"Worker im Subagent-Modus sollten KEIN current_task_id Lock bekommen "
            f"— sie haben parallele Sessions. Got current_task_id={agent.current_task_id}"
        )


@pytest.mark.asyncio
async def test_agent_poll_sets_current_task_id_for_worker_legacy_mode(
    client: AsyncClient, fake_redis, monkeypatch,
):
    """Legacy mode (USE_SUBAGENT_DISPATCH=false): workers get a lock too."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", False)

    raw_token, board_id, agent_id, task_id = await _make_scenario(is_board_lead=False)

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        assert agent.current_task_id == task_id


@pytest.mark.asyncio
async def test_agent_poll_inbox_task_also_sets_lock(
    client: AsyncClient, fake_redis,
):
    """Inbox task gets claimed via poll + current_task_id gets set."""
    raw_token, board_id, agent_id, task_id = await _make_scenario(
        is_board_lead=True, task_status="inbox",
    )

    # Inbox tasks don't need dispatched_at — poll claims directly
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        t.dispatched_at = None
        s.add(t)
        await s.commit()

    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "new_task"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        assert agent.current_task_id == task_id
        task = await s.get(Task, task_id)
        # Plan 26-02 (HERM-10/F1): status stays inbox until agent's own PATCH
        # status:in_progress sets it (Migration 0018 ACK-Handshake). The poll
        # only delivers the prompt + sets dispatched_at + the current_task_id lock.
        assert task.status == "inbox"
        assert task.dispatched_at is not None
