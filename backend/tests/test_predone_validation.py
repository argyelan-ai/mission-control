"""Pre-Done Validation tests (T-1 Phase E)."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


_REFLECTION_BODY = (
    "## Was wurde gemacht\nPredone-Fixture Test\n\n"
    "## Was hat funktioniert\nChecklist-Flow geprueft\n\n"
    "## Was war unklar\nNichts — reiner Validation-Test\n\n"
    "## Lesson fuer Agent-Memory\n"
    "Checklist-Validation laeuft vor Reflection-Guard — beide muessen passieren."
)


async def _post_reflection(client, agent_headers, board_id, task_id):
    """Post the mandatory reflection so the closing transition goes through."""
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        headers=agent_headers,
        json={"content": _REFLECTION_BODY, "comment_type": "reflection"},
    )
    assert resp.status_code in (200, 201), resp.json()


async def _setup_predone_scenario(status: str = "in_progress"):
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
            name="PreDone Board",
            slug=f"pd-{uuid.uuid4().hex[:8]}",
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
            title="PreDone Test Task",
            status=status,
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
async def test_validation_fails_if_checklist_incomplete(client):
    """Agent cannot set a task to done while checklist items are still open."""
    ids = await _setup_predone_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
    board_id = ids["board_id"]
    task_id = ids["task_id"]

    # Create checklist with 2 items
    create_resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist",
        headers=agent_headers,
        json={"items": [
            {"title": "Schritt 1", "sort_order": 0},
            {"title": "Schritt 2", "sort_order": 1},
        ]},
    )
    assert create_resp.status_code == 201

    # Only mark the first item done
    item_id = create_resp.json()[0]["id"]
    await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist/{item_id}",
        headers=agent_headers,
        json={"status": "done"},
    )

    # Try to set to done (must FAIL)
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        headers=agent_headers,
        json={"status": "done"},
    )
    assert resp.status_code == 422, resp.json()
    detail = resp.json()["detail"]
    assert "1 Checklist-Item" in detail or "offen" in detail.lower()


@pytest.mark.asyncio
async def test_validation_fails_if_checklist_incomplete_on_review(client):
    """Agent cannot set a task to review while checklist items are still open."""
    ids = await _setup_predone_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
    board_id = ids["board_id"]
    task_id = ids["task_id"]

    # Create checklist with 2 items — leave both open
    create_resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist",
        headers=agent_headers,
        json={"items": [
            {"title": "Analyse", "sort_order": 0},
            {"title": "Tests schreiben", "sort_order": 1},
        ]},
    )
    assert create_resp.status_code == 201

    # Try to set to review (must FAIL)
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        headers=agent_headers,
        json={"status": "review"},
    )
    assert resp.status_code == 422, resp.json()
    detail = resp.json()["detail"]
    assert "offen" in detail.lower() or "Checklist" in detail


@pytest.mark.asyncio
async def test_validation_passes_if_no_checklist(client):
    """Without a checklist there's no validation block — done is allowed."""
    ids = await _setup_predone_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
    board_id = ids["board_id"]
    task_id = ids["task_id"]

    # Don't create a checklist — go straight to done (post reflection first)
    await _post_reflection(client, agent_headers, board_id, task_id)
    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers=agent_headers,
            json={"status": "done"},
        )
    # Should succeed (no checklist = no validation block)
    assert resp.status_code == 200, resp.json()


@pytest.mark.asyncio
async def test_validation_passes_if_all_checklist_done(client):
    """When all checklist items are done → done is possible."""
    ids = await _setup_predone_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
    board_id = ids["board_id"]
    task_id = ids["task_id"]

    # Create checklist with one item
    create_resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist",
        headers=agent_headers,
        json={"items": [{"title": "Only step", "sort_order": 0}]},
    )
    assert create_resp.status_code == 201
    item_id = create_resp.json()[0]["id"]

    # Mark the item done
    await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist/{item_id}",
        headers=agent_headers,
        json={"status": "done"},
    )

    # Post reflection first (ADR-023) — then set to done
    await _post_reflection(client, agent_headers, board_id, task_id)
    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers=agent_headers,
            json={"status": "done"},
        )
    assert resp.status_code == 200, resp.json()


@pytest.mark.asyncio
async def test_validation_passes_if_all_checklist_done_on_review(client):
    """When all checklist items are done → review is possible."""
    ids = await _setup_predone_scenario()
    agent_headers = {"Authorization": f"Bearer {ids['agent_token']}"}
    board_id = ids["board_id"]
    task_id = ids["task_id"]

    # Create 2 items and set both done
    create_resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist",
        headers=agent_headers,
        json={"items": [
            {"title": "Step A", "sort_order": 0},
            {"title": "Step B", "sort_order": 1},
        ]},
    )
    assert create_resp.status_code == 201
    for item in create_resp.json():
        await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist/{item['id']}",
            headers=agent_headers,
            json={"status": "done"},
        )

    # Add an evidence comment (required by a different guard)
    comment_resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        headers=agent_headers,
        json={"content": "Alles fertig implementiert.", "comment_type": "progress"},
    )
    assert comment_resp.status_code in (200, 201), comment_resp.json()

    # Post reflection (ADR-023 mandatory before closing transition)
    await _post_reflection(client, agent_headers, board_id, task_id)

    # Set to review — must succeed
    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            headers=agent_headers,
            json={"status": "review"},
        )
    assert resp.status_code == 200, resp.json()
