"""Tests fuer Blocker-Approval Guard — PRE-COMMIT Position.

Root Cause: Guard stand NACH session.commit(), dadurch wurde der Status
in der DB geaendert bevor der Guard 403 zurueckgeben konnte.
Fix: Guard steht jetzt VOR setattr/commit in agent_scoped.py
und in _enforce_board_rules() in tasks.py.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

_BROADCAST_PATCH = patch("app.services.activity.broadcast", new_callable=AsyncMock)


async def _setup_blocker_scenario():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.approval import Approval
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    task_id = uuid.uuid4()
    approval_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=board_id, name="Blocker Board", slug=f"blocker-{uuid.uuid4().hex[:8]}",
        )
        s.add(board)

        dev_token, dev_hash = generate_agent_token()
        dev = Agent(
            id=dev_id, name="Sparky", role="developer",
            board_id=board_id, agent_token_hash=dev_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(dev)

        task = Task(
            id=task_id, board_id=board_id,
            title="Blocked Task", status="blocked",
            assigned_agent_id=dev_id,
        )
        s.add(task)

        approval = Approval(
            id=approval_id,
            task_id=task_id,
            board_id=board_id,
            agent_id=dev_id,
            action_type="blocker_decision",
            status="pending",
            description="Test blocker",
        )
        s.add(approval)

        await s.commit()

    return {
        "board_id": board_id,
        "dev_id": dev_id, "dev_token": dev_token,
        "task_id": task_id,
        "approval_id": approval_id,
    }


@pytest.mark.asyncio
async def test_agent_cannot_unblock_with_pending_approval(client):
    """Agent PATCH blocked→in_progress mit pending Approval → 403, DB bleibt blocked."""
    ids = await _setup_blocker_scenario()

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
            headers={"Authorization": f"Bearer {ids['dev_token']}"},
            json={"status": "in_progress"},
        )

    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
    assert "Blocker-Approval" in resp.json()["detail"]

    # DB-Check: Status muss blocked bleiben
    async with AsyncSession(test_engine) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        assert task.status == "blocked", f"DB should still be blocked, got {task.status}"


@pytest.mark.asyncio
async def test_agent_cannot_unblock_with_pending_approval_no_event(client):
    """Kein TaskEvent und kein Activity Event bei blockiertem Entblocken."""
    ids = await _setup_blocker_scenario()

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
            headers={"Authorization": f"Bearer {ids['dev_token']}"},
            json={"status": "in_progress"},
        )

    assert resp.status_code == 403

    # Kein TaskEvent geschrieben
    async with AsyncSession(test_engine) as s:
        from app.models.task import TaskEvent
        from sqlmodel import select
        events = (await s.exec(
            select(TaskEvent).where(TaskEvent.task_id == ids["task_id"])
        )).all()
        assert len(events) == 0, f"Expected 0 events, got {len(events)}"


@pytest.mark.asyncio
async def test_agent_can_unblock_after_approval_resolved(client):
    """Nach Approval-Auflösung kann Agent normal entblocken."""
    ids = await _setup_blocker_scenario()

    # Approval resolven
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.approval import Approval
        approval = await s.get(Approval, ids["approval_id"])
        approval.status = "approved"
        s.add(approval)
        await s.commit()

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
            headers={"Authorization": f"Bearer {ids['dev_token']}"},
            json={"status": "in_progress"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "in_progress"


@pytest.mark.asyncio
async def test_normal_unblock_without_approval_allowed(client):
    """blocked→in_progress ohne pending Approval → erlaubt."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="No Approval Board", slug=f"noappr-{uuid.uuid4().hex[:8]}"))
        dev_token, dev_hash = generate_agent_token()
        s.add(Agent(
            id=dev_id, name="Dev", role="developer",
            board_id=board_id, agent_token_hash=dev_hash,
            scopes=["tasks:read", "tasks:write"],
        ))
        s.add(Task(
            id=task_id, board_id=board_id,
            title="Clean Unblock", status="blocked",
            assigned_agent_id=dev_id,
        ))
        await s.commit()

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {dev_token}"},
            json={"status": "in_progress"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_user_route_also_blocked_by_pending_approval(auth_client):
    """Dashboard-Route blocked→in_progress mit pending Approval → 403."""
    ids = await _setup_blocker_scenario()

    resp = await auth_client.patch(
        f"/api/v1/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        json={"status": "in_progress"},
    )

    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
    assert "Blocker-Approval" in resp.json()["detail"]
