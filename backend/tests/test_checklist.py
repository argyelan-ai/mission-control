"""Tests for TaskChecklistItem CRUD (T-1 Phase B)."""
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_checklist_scenario():
    """Board + Agent (mit tasks:read/write Scopes) + Task anlegen."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=board_id,
            name="Checklist Board",
            slug=f"cl-{uuid.uuid4().hex[:8]}",
        )
        s.add(board)

        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            id=agent_id,
            name="Cody",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(agent)

        task = Task(
            id=task_id,
            board_id=board_id,
            title="Checklist Test Task",
            status="in_progress",
            assigned_agent_id=agent_id,
        )
        s.add(task)
        await s.commit()

    return {
        "board_id": board_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "agent_token": raw_token,
    }


@pytest.mark.asyncio
async def test_agent_can_create_checklist_items(client):
    """Agent kann Checklist-Items für seinen Task anlegen."""
    ids = await _setup_checklist_scenario()

    resp = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers={"Authorization": f"Bearer {ids['agent_token']}"},
        json={"items": [
            {"title": "Analyse", "sort_order": 0},
            {"title": "Tests schreiben", "sort_order": 1},
            {"title": "Implementieren", "sort_order": 2},
        ]},
    )
    assert resp.status_code == 201, resp.json()
    data = resp.json()
    assert len(data) == 3
    assert all(item["status"] == "pending" for item in data)


@pytest.mark.asyncio
async def test_agent_can_update_checklist_item(client):
    """Agent kann ein Item auf done setzen."""
    ids = await _setup_checklist_scenario()

    create_resp = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers={"Authorization": f"Bearer {ids['agent_token']}"},
        json={"items": [{"title": "Schritt 1", "sort_order": 0}]},
    )
    assert create_resp.status_code == 201, create_resp.json()
    item_id = create_resp.json()[0]["id"]

    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist/{item_id}",
        headers={"Authorization": f"Bearer {ids['agent_token']}"},
        json={"status": "done"},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == "done"
    assert resp.json()["completed_at"] is not None


@pytest.mark.asyncio
async def test_checklist_counter_increments_on_done(auth_client):
    """checklist_done auf Task wird erhöht wenn Item auf done gesetzt.

    auth_client wird für den User-GET genutzt.
    Für den Agent-Aufruf überschreiben wir den Authorization-Header per-Request.
    """
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}

    create_resp = await auth_client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [{"title": "Step", "sort_order": 0}]},
    )
    assert create_resp.status_code == 201
    item_id = create_resp.json()[0]["id"]

    await auth_client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist/{item_id}",
        headers=agent_headers,
        json={"status": "done"},
    )

    # User-GET — kein explizites Authorization-Header, auth_client nutzt sein JWT
    task_resp = await auth_client.get(
        f"/api/v1/boards/{ids['board_id']}/tasks/{ids['task_id']}",
    )
    assert task_resp.status_code == 200
    assert task_resp.json()["checklist_done"] == 1
    assert task_resp.json()["checklist_total"] == 1


@pytest.mark.asyncio
async def test_user_can_read_checklist(auth_client):
    """User kann Checklist eines Tasks lesen."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}

    await auth_client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [{"title": "Check 1", "sort_order": 0}]},
    )
    resp = await auth_client.get(
        f"/api/v1/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
    )
    assert resp.status_code == 200, resp.json()
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Check 1"


@pytest.mark.asyncio
async def test_agent_can_read_checklist(client):
    """Agent kann Checklist lesen (für Recovery)."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}

    await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [
            {"title": "A", "sort_order": 0},
            {"title": "B", "sort_order": 1},
        ]},
    )
    resp = await client.get(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
    )
    assert resp.status_code == 200
    titles = [i["title"] for i in resp.json()]
    assert titles == ["A", "B"]


@pytest.mark.asyncio
async def test_checklist_total_counter_increments_on_create(client):
    """checklist_total auf Task wird korrekt hochgezählt."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}

    # Ersten Batch anlegen
    resp1 = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [
            {"title": "Step 1", "sort_order": 0},
            {"title": "Step 2", "sort_order": 1},
        ]},
    )
    assert resp1.status_code == 201

    # Zweiten Batch anlegen
    resp2 = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [{"title": "Step 3", "sort_order": 2}]},
    )
    assert resp2.status_code == 201

    checklist_resp = await client.get(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
    )
    assert checklist_resp.status_code == 200
    assert len(checklist_resp.json()) == 3


@pytest.mark.asyncio
async def test_completed_at_cleared_on_reopen(client):
    """completed_at wird auf None gesetzt wenn Item von done zurück auf pending gesetzt wird."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}

    create_resp = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [{"title": "Reversible Step", "sort_order": 0}]},
    )
    item_id = create_resp.json()[0]["id"]

    # Auf done setzen
    await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist/{item_id}",
        headers=agent_headers,
        json={"status": "done"},
    )

    # Zurück auf pending setzen
    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist/{item_id}",
        headers=agent_headers,
        json={"status": "pending"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert resp.json()["completed_at"] is None
