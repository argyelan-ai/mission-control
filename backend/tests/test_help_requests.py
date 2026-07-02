"""Tests fuer Help Request Endpoint — Agent-zu-Agent Kollaboration."""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from unittest.mock import patch, AsyncMock

from app.models.agent import Agent
from app.models.task import Task
from app.auth import generate_agent_token

from .conftest import test_engine


async def _setup_board(session: AsyncSession):
    """Board erstellen."""
    from app.models.board import Board

    board = Board(id=uuid.uuid4(), name="Test Board", slug="test-board")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def _setup_agent_with_token(
    name: str,
    role: str = "developer",
    board_id: uuid.UUID | None = None,
    scopes: list[str] | None = None,
    provision_status: str = "provisioned",
    current_task_id: uuid.UUID | None = None,
):
    """Agent erstellen + Token-Hash setzen. Gibt (agent, raw_token) zurueck."""
    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=name,
            role=role,
            board_id=board_id,
            scopes=scopes or ["tasks:help"],
            provision_status=provision_status,
            agent_token_hash=token_hash,
            current_task_id=current_task_id,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, raw_token


async def _setup_task(
    board_id: uuid.UUID,
    title: str = "Test Task",
    status: str = "in_progress",
    assigned_agent_id: uuid.UUID | None = None,
    **kwargs,
):
    """Task erstellen."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=uuid.uuid4(),
            board_id=board_id,
            title=title,
            status=status,
            assigned_agent_id=assigned_agent_id,
            **kwargs,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
    return task


@pytest.mark.asyncio
class TestHelpRequest:
    """POST /api/v1/agent/boards/{board_id}/help-request"""

    async def test_successful_help_request(self, client: AsyncClient):
        """201: Subtask erstellt, Sender blockiert."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        # Writer: hat aktiven in_progress Task
        task = await _setup_task(board.id, title="Write article", status="in_progress")
        writer, writer_token = await _setup_agent_with_token(
            name="Writer",
            role="writer",
            board_id=board.id,
            scopes=["tasks:help"],
            current_task_id=task.id,
        )
        # Researcher: provisioniert, frei
        researcher, _ = await _setup_agent_with_token(
            name="Researcher",
            role="researcher",
            board_id=board.id,
            scopes=["tasks:help"],
        )

        # Update task.assigned_agent_id
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = writer.id
            s.add(db_task)
            await s.commit()

        client.headers["Authorization"] = f"Bearer {writer_token}"

        with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
                resp = await client.post(
                    f"/api/v1/agent/boards/{board.id}/help-request",
                    json={
                        "needed_role": "researcher",
                        "title": "Research topic X",
                        "context": "I need info about topic X",
                    },
                )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "help_task_id" in data
        assert data["assigned_to"] == "Researcher"
        assert data["your_status"] == "blocked"

        # Verify: sender task is now blocked
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            sender_task = await s.get(Task, task.id)
            assert sender_task.status == "blocked"
            assert sender_task.blocked_by_task_id is not None

            # Verify subtask exists
            subtask = await s.get(Task, uuid.UUID(data["help_task_id"]))
            assert subtask is not None
            assert subtask.help_request_from == writer.id
            assert subtask.assigned_agent_id == researcher.id

    async def test_depth_limit_rejects_nested_help(self, client: AsyncClient):
        """403: Task ist bereits ein Help Request — keine Verschachtelung."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        # Task der selbst ein help_request ist
        dummy_agent_id = uuid.uuid4()
        task = await _setup_task(
            board.id,
            title="Already a help task",
            status="in_progress",
            help_request_from=dummy_agent_id,
        )

        agent, token = await _setup_agent_with_token(
            name="Helper",
            role="developer",
            board_id=board.id,
            scopes=["tasks:help"],
            current_task_id=task.id,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/help-request",
            json={
                "needed_role": "researcher",
                "title": "Nested help",
                "context": "This should fail",
            },
        )

        assert resp.status_code == 403
        assert "verschachtelt" in resp.json()["detail"].lower()

    async def test_agent_must_be_in_progress(self, client: AsyncClient):
        """409: Agent hat keinen aktiven in_progress Task."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        # Agent ohne current_task_id
        agent, token = await _setup_agent_with_token(
            name="Idle Agent",
            role="writer",
            board_id=board.id,
            scopes=["tasks:help"],
            current_task_id=None,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/help-request",
            json={
                "needed_role": "researcher",
                "title": "Should fail",
                "context": "No active task",
            },
        )

        assert resp.status_code == 409

    async def test_no_agent_with_needed_role(self, client: AsyncClient):
        """404: Kein provisionierter Agent mit der benoetigten Rolle."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, title="Active task", status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="Writer",
            role="writer",
            board_id=board.id,
            scopes=["tasks:help"],
            current_task_id=task.id,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/help-request",
            json={
                "needed_role": "nonexistent_role",
                "title": "Need help",
                "context": "No one has this role",
            },
        )

        assert resp.status_code == 404
        assert "nonexistent_role" in resp.json()["detail"]
