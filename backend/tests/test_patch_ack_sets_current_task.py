"""Tests for bug fix 2026-04-24: push-dispatch ACK (PATCH status:in_progress) must
set agent.current_task_id, otherwise 409 on `mc delegate` / `mc help-request` /
`mc clarification`.

Live bug: Boss received a task via push-dispatch, ACKed via PATCH, but
current_task_id stayed null. mc delegate → 409 "No active task". Workaround was
direct POST /tasks. Fix: PATCH ACK sets current_task_id analogous to pull-dispatch.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent_with_task(
    *,
    is_board_lead: bool = True,
    use_subagent_dispatch: bool = False,
    task_status: str = "inbox",
):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="TestBoard", slug=f"tb-{uuid.uuid4().hex[:6]}"))
        await s.commit()

        agent = Agent(
            id=uuid.uuid4(),
            name="TestBoss",
            role="orchestrator",
            is_board_lead=is_board_lead,
            board_id=board_id,
            agent_runtime="host",
            agent_token_hash=token_hash,
            scopes=["heartbeat", "tasks:read", "tasks:write"],
            current_task_id=None,
            model="x",
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

        task = Task(
            board_id=board_id,
            title="Pushed Task",
            description="x",
            status=task_status,
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

    return agent, raw_token, board_id, task


@pytest.mark.asyncio
async def test_patch_ack_sets_current_task_id_for_board_lead(client: AsyncClient):
    """Push-dispatch board lead: PATCH status:in_progress → current_task_id set."""
    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=True)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id, (
            f"current_task_id nicht gesetzt — Bug nicht gefixt! "
            f"current_task_id={fresh.current_task_id}, expected={task.id}"
        )


@pytest.mark.asyncio
async def test_patch_ack_sets_current_task_id_for_worker_when_subagent_off(
    client: AsyncClient, monkeypatch,
):
    """Legacy mode (USE_SUBAGENT_DISPATCH=false): workers get the lock too."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", False)

    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=False)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id


@pytest.mark.asyncio
async def test_patch_ack_skips_current_task_id_for_worker_subagent_mode(
    client: AsyncClient, monkeypatch,
):
    """Subagent-dispatch mode: workers have parallel sessions, current_task_id
    stays null (like pull-dispatch agent_scoped.py:1293)."""
    from app.config import settings
    monkeypatch.setattr(settings, "use_subagent_dispatch", True)

    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=False)

    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id is None, (
            f"Worker in Subagent-Modus soll kein current_task_id bekommen — "
            f"parallele Sessions brauchen den Lock nicht. "
            f"current_task_id={fresh.current_task_id}"
        )


@pytest.mark.asyncio
async def test_patch_done_clears_current_task_id(client: AsyncClient):
    """When current_task_id is set and the task becomes done → release the lock."""
    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=True)

    # 1. ACK (sets current_task_id)
    r1 = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    assert r1.status_code == 200

    # 2. Done (clear)
    r2 = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "done"},
    )
    assert r2.status_code == 200, r2.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id is None, (
            f"Lock muss bei done freigegeben werden — "
            f"current_task_id={fresh.current_task_id}"
        )


@pytest.mark.asyncio
async def test_auto_ack_via_comment_sets_current_task_id(client: AsyncClient):
    """Auto-ACK via comment (ack_at=None + dispatched_at != None) → set current_task_id.

    Bug repro 2026-04-24 (Boss Acme-Corp brief): Boss ACKed via the first
    comment (auto-ACK path, NOT via PATCH), current_task_id stayed null,
    mc delegate threw 409. PR #103 only fixed the PATCH path — this covers
    the comment auto-ACK path too.
    """
    agent, token, board_id, task = await _make_agent_with_task(
        is_board_lead=True, task_status="inbox",
    )
    # Set dispatched marker (otherwise no auto-ACK)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task as _Task
        fresh_task = await s.get(_Task, task.id)
        from datetime import datetime, timezone
        fresh_task.dispatched_at = datetime.now(tz=timezone.utc)
        s.add(fresh_task)
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}/comments",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "First comment triggers Auto-ACK"},
    )
    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id, (
            f"Auto-ACK muss current_task_id setzen — sonst scheitert mc delegate. "
            f"Got current_task_id={fresh.current_task_id}"
        )


@pytest.mark.asyncio
async def test_patch_ack_idempotent_for_already_locked_task(client: AsyncClient):
    """When current_task_id is already this task (e.g. via pull-dispatch), no overwrite."""
    agent, token, board_id, task = await _make_agent_with_task(is_board_lead=True, task_status="in_progress")

    # Simulate pull-dispatch state: current_task_id already set
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        fresh.current_task_id = task.id
        s.add(fresh)
        await s.commit()

    # Agent sends PATCH status:in_progress even though already in_progress (retry)
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "in_progress"},
    )
    # We accept 200 (no-op) or 400/409 (status-transition guard).
    # What matters is that current_task_id stays intact, regardless of what the server does.
    assert resp.status_code in (200, 400, 409), resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        assert fresh.current_task_id == task.id
