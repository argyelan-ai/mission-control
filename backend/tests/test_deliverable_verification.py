"""Tests: Deliverable-Verifikations-Endpoints (Bug A + E Fix).

Incident-Context 2026-04-23 (Root-Task e00d2932 "Kleine Rescherche"):
Agents konnten nicht verifizieren dass `content` nach POST /deliverables
gespeichert wurde — der LIST-Endpoint blendete `content` aus, einen Single-
GET gab es nicht. Resultat: Researcher re-registrierte 4x dasselbe
Deliverable, Boss triggerte den done→inbox-Crash beim phase_rewrite.

Diese Tests decken ab:
  - Bug A: `?include_content=true` im LIST, neuer Single-GET Endpoint
  - Bug E: Dedup im POST (gleicher agent+task+path returned existing)
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_agent_and_task():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="DelivVerify", slug=f"dv-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Resercha", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Research Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()
    return board_id, agent_id, task_id, token_raw


# ── LIST with ?include_content ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_deliverables_omits_content_by_default(client, fake_redis):
    """LIST response hat kein content-Feld aber content_length zeigt Groesse."""
    from app.models.deliverable import TaskDeliverable
    board_id, agent_id, task_id, token = await _setup_agent_and_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskDeliverable(
            task_id=task_id, agent_id=agent_id,
            deliverable_type="document",
            title="Test Report",
            content="# Sehr langer Markdown-Content " + "x" * 1000,
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "content" not in data[0], "LIST ohne include_content darf kein content returnieren"
    assert data[0]["content_length"] > 1000, "content_length zeigt aber die echte Groesse"


@pytest.mark.asyncio
async def test_list_deliverables_with_include_content_returns_full_body(client, fake_redis):
    """LIST mit ?include_content=true liefert den vollen content-Body."""
    from app.models.deliverable import TaskDeliverable
    board_id, agent_id, task_id, token = await _setup_agent_and_task()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskDeliverable(
            task_id=task_id, agent_id=agent_id,
            deliverable_type="document",
            title="Full Content Test",
            content="FULL MARKDOWN BODY HERE 12345",
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables?include_content=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["content"] == "FULL MARKDOWN BODY HERE 12345"
    assert data[0]["content_length"] == 29


# ── Single-GET Endpoint ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_get_deliverable_returns_content(client, fake_redis):
    """Neuer Single-GET Endpoint gibt content + meta-fields zurueck."""
    from app.models.deliverable import TaskDeliverable
    board_id, agent_id, task_id, token = await _setup_agent_and_task()

    deliv_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskDeliverable(
            id=deliv_id,
            task_id=task_id, agent_id=agent_id,
            deliverable_type="document",
            title="Verifizierbares Deliverable",
            content="Dieser Text muss im GET zurueckkommen.",
            path=None,
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables/{deliv_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["content"] == "Dieser Text muss im GET zurueckkommen."
    assert data["content_length"] == 38
    assert data["title"] == "Verifizierbares Deliverable"
    assert data["deliverable_type"] == "document"


@pytest.mark.asyncio
async def test_single_get_deliverable_404_for_wrong_task(client, fake_redis):
    """Deliverable gehoert zu anderem Task → 404."""
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task
    board_id, agent_id, task_id, token = await _setup_agent_and_task()

    other_task = uuid.uuid4()
    deliv_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=other_task, board_id=board_id, title="Other",
            status="in_progress", assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        s.add(TaskDeliverable(
            id=deliv_id,
            task_id=other_task,  # BELONGS TO OTHER TASK
            agent_id=agent_id,
            deliverable_type="document",
            title="Fremd",
            content="x",
        ))
        await s.commit()

    # Zugriff via task_id (nicht other_task) muss 404 geben
    resp = await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables/{deliv_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert "gehoert nicht zu Task" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_single_get_deliverable_404_for_missing(client, fake_redis):
    """Nicht-existierende deliverable_id → 404."""
    board_id, agent_id, task_id, token = await _setup_agent_and_task()
    resp = await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ── Bug E: Dedup im POST ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_deliverable_dedup_returns_existing(client, fake_redis, tmp_path):
    """Zweiter POST mit gleichem path+title+agent returniert existierendes Deliverable."""
    from app.models.deliverable import TaskDeliverable
    board_id, agent_id, task_id, token = await _setup_agent_and_task()

    body = {
        "deliverable_type": "document",
        "title": "Research Report",
        "path": f"/deliverables/{task_id}/report.md",
        "content": "# First Version",
    }

    r1 = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code in (200, 201), r1.text
    first_id = r1.json()["id"]
    assert not r1.json().get("duplicate")

    # Zweiter Call mit gleichem path → Dedup
    r2 = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
        json={**body, "content": "# Second Version — sollte ignored werden"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code in (200, 201)
    assert r2.json()["id"] == first_id, "Dedup muss existierende ID returnieren"
    assert r2.json().get("duplicate") is True
    assert "existiert bereits" in r2.json().get("message", "").lower()


@pytest.mark.asyncio
async def test_create_deliverable_dedup_per_agent(client, fake_redis):
    """Anderer Agent auf gleichem Task+Path ist KEIN Duplikat (legitime Cross-Agent-Beitraege)."""
    from app.models.deliverable import TaskDeliverable
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id, agent_a_id, task_id, token_a = await _setup_agent_and_task()

    # Zweiter Agent auf gleichem Board + Task
    agent_b_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        token_b_raw, token_b_hash = generate_agent_token()
        s.add(Agent(
            id=agent_b_id, name="SecondAgent", role="developer",
            board_id=board_id, agent_token_hash=token_b_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        await s.commit()

    body = {
        "deliverable_type": "document",
        "title": "Shared Title",
        "path": f"/deliverables/{task_id}/shared.md",
        "content": "Agent A version",
    }
    r1 = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
        json=body,
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r1.status_code in (200, 201)
    assert not r1.json().get("duplicate")

    # Agent B postet gleichen path → soll NEU anlegen (kein Dedup)
    body_b = {**body, "content": "Agent B version"}
    r2 = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
        json=body_b,
        headers={"Authorization": f"Bearer {token_b_raw}"},
    )
    assert r2.status_code in (200, 201)
    assert r2.json()["id"] != r1.json()["id"], "Cross-Agent darf Duplikate haben"
    assert not r2.json().get("duplicate")
