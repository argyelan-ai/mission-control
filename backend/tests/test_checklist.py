"""Tests for TaskChecklistItem CRUD (T-1 Phase B)."""
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_checklist_scenario():
    """Create board + agent (with tasks:read/write scopes) + task."""
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
    """Agent can create checklist items for its task."""
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
    """Agent can set an item to done."""
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
    """checklist_done on task is incremented when item is set to done.

    auth_client is used for the user GET.
    For the agent call we override the Authorization header per-request.
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

    # User GET — no explicit Authorization header, auth_client uses its JWT
    task_resp = await auth_client.get(
        f"/api/v1/boards/{ids['board_id']}/tasks/{ids['task_id']}",
    )
    assert task_resp.status_code == 200
    assert task_resp.json()["checklist_done"] == 1
    assert task_resp.json()["checklist_total"] == 1


@pytest.mark.asyncio
async def test_user_can_read_checklist(auth_client):
    """User can read a task's checklist."""
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
    """Agent can read checklist (for recovery)."""
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
    """checklist_total on task is correctly incremented."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}

    # Create first batch
    resp1 = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [
            {"title": "Step 1", "sort_order": 0},
            {"title": "Step 2", "sort_order": 1},
        ]},
    )
    assert resp1.status_code == 201

    # Create second batch
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
    """completed_at is set to None when item is moved from done back to pending."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}

    create_resp = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json={"items": [{"title": "Reversible Step", "sort_order": 0}]},
    )
    item_id = create_resp.json()[0]["id"]

    # Set to done
    await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist/{item_id}",
        headers=agent_headers,
        json={"status": "done"},
    )

    # Set back to pending
    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist/{item_id}",
        headers=agent_headers,
        json={"status": "pending"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert resp.json()["completed_at"] is None


@pytest.mark.asyncio
async def test_checklist_create_is_idempotent_on_redispatch(client):
    """Re-dispatch (container restart, manual resume, blocked→in_progress unblock)
    re-instructs the agent to create its checklist. Replaying the exact same
    bulk-create payload must NOT duplicate rows — this was the real incident
    root cause (one item created 3x, others 2x, which later made `mc finish`
    fail with a wall of open items)."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
    payload = {"items": [
        {"title": "Analyse", "sort_order": 0},
        {"title": "Tests schreiben", "sort_order": 1},
        {"title": "Implementieren", "sort_order": 2},
    ]}

    first = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json=payload,
    )
    assert first.status_code == 201, first.json()
    assert len(first.json()) == 3

    # Simulated re-dispatch: agent replays the identical "create checklist" step.
    second = await client.post(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
        json=payload,
    )
    assert second.status_code == 200, second.json()
    assert len(second.json()) == 3

    list_resp = await client.get(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist",
        headers=agent_headers,
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 3  # NOT doubled to 6

    task_resp = await client.get(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}",
        headers=agent_headers,
    )
    assert task_resp.status_code == 200
    assert task_resp.json()["checklist_total"] == 3  # NOT inflated to 6


@pytest.mark.asyncio
async def test_checklist_dedup_scoped_to_non_terminal_items(client):
    """Dedup must only fire against still-open (pending/in_progress) items.

    If an item titled "Run tests" is already done and the agent legitimately
    wants a NEW round of "Run tests", the POST must create a fresh pending
    item — not silently return the old done row (which would hide the new
    work from `mc finish` and recovery)."""
    ids = await _setup_checklist_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
    base = f"/api/v1/agent/boards/{ids['board_id']}/tasks/{ids['task_id']}/checklist"

    create = await client.post(
        base, headers=agent_headers,
        json={"items": [{"title": "Run tests", "sort_order": 0}]},
    )
    assert create.status_code == 201
    item_id = create.json()[0]["id"]

    # Mark it done — it is now a terminal item.
    done = await client.patch(
        f"{base}/{item_id}", headers=agent_headers, json={"status": "done"},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "done"

    # Agent wants a new round of "Run tests" → a fresh pending item must appear.
    again = await client.post(
        base, headers=agent_headers,
        json={"items": [{"title": "Run tests", "sort_order": 1}]},
    )
    assert again.status_code == 201, again.json()
    assert len(again.json()) == 1
    assert again.json()[0]["status"] == "pending"
    assert again.json()[0]["id"] != item_id

    # Two rows titled "Run tests" now exist: one done, one pending.
    list_resp = await client.get(base, headers=agent_headers)
    assert list_resp.status_code == 200
    run_tests = [i for i in list_resp.json() if i["title"] == "Run tests"]
    assert len(run_tests) == 2
    statuses = sorted(i["status"] for i in run_tests)
    assert statuses == ["done", "pending"]
