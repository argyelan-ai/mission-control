"""Tests for the clarification endpoint — agent asks the operator clarifying questions."""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from unittest.mock import patch, AsyncMock

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.task import Task
from app.auth import generate_agent_token

from .conftest import test_engine


async def _setup_board(session: AsyncSession):
    """Create board."""
    from app.models.board import Board

    board = Board(id=uuid.uuid4(), name="Test Board", slug="test-board-clar")
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
    """Create agent + set token hash. Returns (agent, raw_token)."""
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
    """Create task."""
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
class TestClarification:
    """POST /api/v1/agent/boards/{board_id}/clarification"""

    async def test_clarification_creates_approval_and_blocks_agent(self, client: AsyncClient):
        """201: approval created, task blocked."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, title="Implement feature", status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="Cody",
            role="developer",
            board_id=board.id,
            scopes=["tasks:help"],
            current_task_id=task.id,
        )

        # Update task.assigned_agent_id
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()

        client.headers["Authorization"] = f"Bearer {token}"

        with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/agent/boards/{board.id}/clarification",
                json={
                    "question": "Soll ich Redis oder PostgreSQL fuer den Cache nutzen?",
                    "options": ["Redis", "PostgreSQL"],
                },
            )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "approval_id" in data
        assert data["your_status"] == "blocked"

        # Verify: task is now blocked
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            blocked_task = await s.get(Task, task.id)
            assert blocked_task.status == "blocked"

            # Verify: approval exists with correct payload
            approval = await s.get(Approval, uuid.UUID(data["approval_id"]))
            assert approval is not None
            assert approval.action_type == "clarification_question"
            assert approval.status == "pending"
            assert approval.task_id == task.id
            assert approval.agent_id == agent.id
            assert approval.payload["question"] == "Soll ich Redis oder PostgreSQL fuer den Cache nutzen?"
            assert approval.payload["options"] == ["Redis", "PostgreSQL"]
            assert approval.payload["task_title"] == "Implement feature"
            assert approval.payload["agent_name"] == "Cody"

    async def test_clarification_requires_in_progress(self, client: AsyncClient):
        """409: agent has no active in_progress task."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        # Agent without current_task_id
        agent, token = await _setup_agent_with_token(
            name="Idle Agent",
            role="developer",
            board_id=board.id,
            scopes=["tasks:help"],
            current_task_id=None,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/clarification",
            json={
                "question": "Was soll ich tun?",
            },
        )

        assert resp.status_code == 409
