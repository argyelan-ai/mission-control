"""Tests: `/agent/tasks/{task_id}/pdf` Endpoint + `mc pdf` CLI.

Unit-Tests mit gemocktem mc-playwright Sidecar. E2E-Tests gegen den echten
Sidecar laufen separat (require docker-compose up + shared volume).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_agent_task():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="PdfGen", slug=f"pdf-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="PdfAgent", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="PDF Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()
    return board_id, agent_id, task_id, token_raw


@pytest.mark.asyncio
async def test_pdf_endpoint_happy_path_registers_deliverable(client, fake_redis):
    """POST /pdf mit markdown → Sidecar-Call + Deliverable erstellt."""
    from app.models.deliverable import TaskDeliverable
    from sqlmodel import select

    board_id, agent_id, task_id, token = await _setup_agent_task()

    fake_sidecar_response = {
        "path": f"/shared-deliverables/{task_id}/report.pdf",
        "bytes": 15000,
        "title": "Q1 Report",
        "task_id": str(task_id),
        "pages": 6,
    }

    with patch("app.services.pdf_generator.generate_pdf", AsyncMock(return_value=fake_sidecar_response)):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/pdf",
            json={"title": "Q1 Report", "markdown": "# Report\n\nHello"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["title"] == "Q1 Report"
    assert data["path"].endswith("report.pdf")
    deliv_id = data["deliverable_id"]

    # Deliverable in DB persistiert
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        d = await s.get(TaskDeliverable, uuid.UUID(deliv_id))
        assert d is not None
        assert d.deliverable_type == "file"
        assert d.title == "Q1 Report"
        assert d.task_id == task_id
        assert d.agent_id == agent_id


@pytest.mark.asyncio
async def test_pdf_endpoint_rejects_both_markdown_and_html(client, fake_redis):
    """markdown + html gleichzeitig → 422."""
    board_id, _, task_id, token = await _setup_agent_task()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/pdf",
        json={"title": "X", "markdown": "# x", "html": "<p>x</p>"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "schliessen sich aus" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_pdf_endpoint_rejects_neither_markdown_nor_html(client, fake_redis):
    """weder markdown noch html → 422."""
    board_id, _, task_id, token = await _setup_agent_task()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/pdf",
        json={"title": "X"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_pdf_endpoint_404_for_unknown_task(client, fake_redis):
    board_id, _, _, token = await _setup_agent_task()
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{uuid.uuid4()}/pdf",
        json={"title": "X", "markdown": "# x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pdf_endpoint_rejects_cross_board_agent(client, fake_redis):
    """Agent auf anderem Board → 403."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_a, _, task_a, _ = await _setup_agent_task()

    # Agent auf Board B
    board_b = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_b, name="Other", slug=f"oth-{uuid.uuid4().hex[:6]}"))
        token_b_raw, token_b_hash = generate_agent_token()
        s.add(Agent(
            id=other_agent_id, name="Outside", role="developer",
            board_id=board_b, agent_token_hash=token_b_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
        ))
        await s.commit()

    # Agent B versucht Task auf Board A zu adressieren
    resp = await client.post(
        f"/api/v1/agent/boards/{board_a}/tasks/{task_a}/pdf",
        json={"title": "X", "markdown": "# x"},
        headers={"Authorization": f"Bearer {token_b_raw}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pdf_endpoint_sidecar_failure_returns_503(client, fake_redis):
    """Wenn Sidecar nicht erreichbar → 503 mit Hinweis auf docker ps."""
    import httpx
    board_id, _, task_id, token = await _setup_agent_task()

    async def _raise_conn_err(*a, **kw):
        raise httpx.ConnectError("Connection refused")

    with patch("app.services.pdf_generator.generate_pdf", side_effect=_raise_conn_err):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/pdf",
            json={"title": "X", "markdown": "# x"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 503
    assert "mc-playwright" in resp.json()["detail"].lower() or "sidecar" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_pdf_endpoint_requires_tasks_write_scope(client, fake_redis):
    """Agent ohne tasks:write → 403."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="NoScope", slug=f"ns-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="ReadOnly", role="researcher",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read"],  # KEIN tasks:write
            provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="X",
            status="in_progress", assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/pdf",
        json={"title": "X", "markdown": "# x"},
        headers={"Authorization": f"Bearer {token_raw}"},
    )
    assert resp.status_code == 403
