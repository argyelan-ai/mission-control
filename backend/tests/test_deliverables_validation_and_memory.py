"""Tests for the deliverable endpoint:
- Path validation: only /deliverables/<task_id>/ or content-only
- Auto memory write: every deliverable creates a BoardMemory entry
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_test_data(session: AsyncSession):
    """Board + agent (with tasks:write scope) + task with in_progress."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    board = Board(id=board_id, name="Test Board", slug="test")
    session.add(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=agent_id,
        name="Davinci",
        board_id=board_id,
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write", "knowledge:read", "knowledge:write"],
    )
    session.add(agent)

    task = Task(
        id=task_id,
        board_id=board_id,
        title="Test Task",
        status="in_progress",
        assigned_agent_id=agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(board)
    await session.refresh(agent)
    await session.refresh(task)
    return board, agent, task, raw_token


# ── Path validation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliverable_rejects_path_outside_deliverables_dir(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "deliverable_type": "file",
            "title": "Asset",
            "path": "/home/agent/bad.png",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
    assert "/deliverables/" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_deliverable_rejects_home_freecode_path(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "deliverable_type": "file",
            "title": "Asset",
            "path": "~/FreeCode/projects/foo/asset.png",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_deliverable_accepts_mounted_path(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "file",
                "title": "Asset",
                "path": f"/deliverables/{task.id}/asset.png",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_deliverable_content_only_allowed_without_path(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "document",
                "title": "Inline Report",
                "content": "# Report\n\nDetails...",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_deliverable_empty_without_path_or_content_rejected(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    resp = await client.post(
        f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
        json={
            "deliverable_type": "file",
            "title": "Empty",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_deliverable_url_type_accepts_https(client: AsyncClient):
    """URL deliverables may still have https://... in the path."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "url",
                "title": "Deploy",
                "path": "https://example.vercel.app",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


# ── Auto memory write ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliverable_creates_board_memory_entry(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "document",
                "title": "Research: Higgsfield Prompts",
                "content": "## Frame 1\nPrompt: ...",
                "tags": ["research"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text

    # Check BoardMemory entry
    from app.models.memory import BoardMemory
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        r = await s.exec(select(BoardMemory).where(BoardMemory.board_id == board.id))
        entries = list(r.all())
    assert len(entries) == 1
    e = entries[0]
    assert e.title == "Research: Higgsfield Prompts"
    assert e.memory_type == "knowledge"     # document → knowledge
    assert e.agent_id == agent.id
    assert e.source == agent.name
    assert e.auto_generated is True
    assert f"task:{task.id}" in e.tags
    assert any(t.startswith("deliverable:") for t in e.tags)


@pytest.mark.asyncio
async def test_deliverable_file_maps_to_reference_memory(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board, agent, task, token = await _create_test_data(s)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}/deliverables",
            json={
                "deliverable_type": "file",
                "title": "Asset PNG",
                "path": f"/deliverables/{task.id}/asset.png",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text

    from app.models.memory import BoardMemory
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        r = await s.exec(select(BoardMemory).where(BoardMemory.board_id == board.id))
        entries = list(r.all())
    assert len(entries) == 1
    assert entries[0].memory_type == "reference"
    assert f"/deliverables/{task.id}/asset.png" in entries[0].content
