"""Tests for `mc ask` — POST /api/v1/agent/tasks/current/ask (Task 7).

Both stages: non-blocking (question posted, task untouched) and blocking
(question posted + status -> waiting + system line). Mirrors the
test_clarification.py fixture pattern.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.models.agent import Agent
from app.models.task import Task
from app.models.thread import Thread, Message
from app.auth import generate_agent_token

from .conftest import test_engine


async def _setup_board(session: AsyncSession):
    from app.models.board import Board

    board = Board(id=uuid.uuid4(), name="Test Board", slug="test-board-ask")
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
    comm_v2: bool = True,
):
    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=name,
            role=role,
            board_id=board_id,
            scopes=scopes or ["chat:write"],
            provision_status=provision_status,
            agent_token_hash=token_hash,
            current_task_id=current_task_id,
            comm_v2=comm_v2,
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
class TestMcAsk:
    """POST /api/v1/agent/tasks/current/ask"""

    async def test_non_blocking_posts_question_task_stays_in_progress(self, client: AsyncClient):
        """(a) non-blocking: task stays in_progress, question message with awaiting=True exists."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="Cody", board_id=board.id, current_task_id=task.id,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={"question": "Redis oder Postgres fuer den Cache?"},
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["your_status"] == "in_progress"

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            still_in_progress = await s.get(Task, task.id)
            assert still_in_progress.status == "in_progress"

            result = await s.exec(select(Message).where(Message.id == uuid.UUID(data["message_id"])))
            message = result.one()
            assert message.message_type == "question"
            assert message.body == "Redis oder Postgres fuer den Cache?"
            assert message.question_meta["awaiting"] is True
            assert message.question_meta["to"] == "boss"
            assert message.question_meta["priority"] == "medium"

    async def test_blocking_sets_waiting_and_system_message(self, client: AsyncClient):
        """(b) blocking: status -> waiting + system message."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="Rex", board_id=board.id, current_task_id=task.id,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={"question": "Deploy jetzt?", "blocking": True},
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["your_status"] == "waiting"

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            waiting_task = await s.get(Task, task.id)
            assert waiting_task.status == "waiting"

            thread_result = await s.exec(select(Thread).where(Thread.task_id == task.id))
            thread = thread_result.one()
            msgs_result = await s.exec(
                select(Message).where(Message.thread_id == thread.id).order_by(Message.seq)
            )
            messages = msgs_result.all()
            assert any(m.message_type == "question" for m in messages)
            system_msgs = [m for m in messages if m.message_type == "system"]
            assert len(system_msgs) == 1
            assert "Rex" in system_msgs[0].body
            assert "wartet auf Antwort" in system_msgs[0].body
            assert "blocking" in system_msgs[0].body

    async def test_requires_chat_write_scope(self, client: AsyncClient):
        """(c) question without chat:write scope -> 403."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="NoScope", board_id=board.id, current_task_id=task.id, scopes=["tasks:read"],
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={"question": "Darf ich das?"},
        )

        assert resp.status_code == 403

    async def test_options_and_default_land_in_question_meta(self, client: AsyncClient):
        """(d) options/default land in question_meta."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="Cody", board_id=board.id, current_task_id=task.id,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={
                "question": "Welche Option?",
                "options": ["A", "B"],
                "default": "A",
                "priority": "high",
                "to": "mark",
                "deadline": "2026-07-20T12:00:00Z",
            },
        )

        assert resp.status_code == 201, resp.text
        data = resp.json()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await s.exec(select(Message).where(Message.id == uuid.UUID(data["message_id"])))
            message = result.one()
            assert message.question_meta["options"] == ["A", "B"]
            assert message.question_meta["default"] == "A"
            assert message.question_meta["priority"] == "high"
            assert message.question_meta["to"] == "mark"
            assert message.question_meta["deadline"] == "2026-07-20T12:00:00Z"

    async def test_no_current_task_returns_409(self, client: AsyncClient):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        agent, token = await _setup_agent_with_token(
            name="Idle", board_id=board.id, current_task_id=None,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={"question": "Was soll ich tun?"},
        )

        assert resp.status_code == 409

    async def test_blocking_rejected_for_non_comm_v2_agent(self, client: AsyncClient):
        """(A2) blocking ask from a non-pilot agent -> 403. Delivery of the answer
        is comm_v2-gated, so a non-pilot parking in `waiting` could never be
        released (dead task)."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="NonPilot", board_id=board.id, current_task_id=task.id, comm_v2=False,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={"question": "Deploy jetzt?", "blocking": True},
        )

        assert resp.status_code == 403, resp.text
        assert "messaging v2 pilot" in resp.json()["detail"]

        # Task was NOT moved to waiting — the gate rejected before any mutation.
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            unchanged = await s.get(Task, task.id)
            assert unchanged.status == "in_progress"

    async def test_non_blocking_allowed_for_non_comm_v2_agent(self, client: AsyncClient):
        """(A2) non-blocking ask is harmless for a non-pilot — question lands in
        the thread (visible in web), task untouched. Allowed for all."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, status="in_progress")
        agent, token = await _setup_agent_with_token(
            name="NonPilot2", board_id=board.id, current_task_id=task.id, comm_v2=False,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={"question": "Nur eine Frage."},
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["your_status"] == "in_progress"

    async def test_blocking_requires_in_progress(self, client: AsyncClient):
        """blocking=True from a non-in_progress task is rejected (VALID_TRANSITIONS)."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _setup_board(s)

        task = await _setup_task(board.id, status="review")
        agent, token = await _setup_agent_with_token(
            name="Cody", board_id=board.id, current_task_id=task.id,
        )

        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            "/api/v1/agent/tasks/current/ask",
            json={"question": "Deploy jetzt?", "blocking": True},
        )

        assert resp.status_code == 409
