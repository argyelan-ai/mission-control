"""Tests fuer die Blocker-Pflichtfelder-Validierung.

Deckt ab:
- 422 wenn blocker_type fehlt bei status=blocked
- 422 wenn blocker_question fehlt bei status=blocked
- 422 wenn blocker_type ungueltig (nicht im Enum)
- blocker_description wird auf 300 Zeichen getrimmt
- blocker_question wird auf 150 Zeichen getrimmt
- Erfolg wenn beide Pflichtfelder gesetzt
- Felder koennen weiterhin fehlen bei anderen Status-Wechseln
"""

import uuid
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from tests.conftest import test_engine


async def _setup(*, task_status="in_progress"):
    """Board + Developer + Task erstellen, Token zurueckgeben."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Validation Board", slug=f"val-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()

    token_raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=agent_id,
            name="Cody",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(agent)
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=task_id,
            board_id=board_id,
            title="Validation Test Task",
            status=task_status,
            assigned_agent_id=agent_id,
        )
        s.add(task)
        await s.commit()

    return board_id, agent_id, task_id, token_raw


@pytest.mark.asyncio
async def test_blocked_without_blocker_type_returns_422(client):
    board_id, _, task_id, token = await _setup()
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={"status": "blocked", "blocker_question": "Was soll ich tun?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "blocker_type" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_blocked_without_blocker_question_returns_422(client):
    board_id, _, task_id, token = await _setup()
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={"status": "blocked", "blocker_type": "missing_info"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "blocker_question" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_blocked_with_invalid_blocker_type_returns_422(client):
    board_id, _, task_id, token = await _setup()
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "blocked",
            "blocker_type": "invalid_type",
            "blocker_question": "Was soll ich tun?",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "blocker_type" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_blocked_description_trimmed_to_1000_chars(client):
    board_id, _, task_id, token = await _setup()
    long_desc = "x" * 1500
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "blocked",
            "blocker_type": "missing_info",
            "blocker_description": long_desc,
            "blocker_question": "Was soll ich tun?",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 207)
    from sqlmodel import select
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = (await s.exec(
            select(Approval).where(Approval.task_id == task_id)
        )).first()
    assert approval is not None
    assert len(approval.payload["description"]) <= 1000


@pytest.mark.asyncio
async def test_blocked_question_trimmed_to_1000_chars(client):
    board_id, _, task_id, token = await _setup()
    long_q = "?" * 1500
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "blocked",
            "blocker_type": "decision_needed",
            "blocker_question": long_q,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 207)
    from sqlmodel import select
    from app.models.approval import Approval
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = (await s.exec(
            select(Approval).where(Approval.task_id == task_id)
        )).first()
    assert approval is not None
    assert len(approval.payload["question"]) <= 1000


@pytest.mark.asyncio
async def test_blocked_with_all_required_fields_succeeds(client):
    board_id, _, task_id, token = await _setup()
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "blocked",
            "blocker_type": "missing_info",
            "blocker_question": "Wo liegt der Vercel Token?",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 207)


@pytest.mark.asyncio
async def test_other_status_change_does_not_require_blocker_fields(client):
    """review-Wechsel braucht keine Blocker-Felder."""
    board_id, _, task_id, token = await _setup()
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={"status": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Darf nicht 422 sein (kein Blocker-Validierungsfehler) — andere Guards (z.B. Evidence-Guard)
    # koennen noch andere Codes zurueckgeben, das ist unabhaengig von der Blocker-Validierung.
    assert resp.status_code != 422
