"""Tests for Task 8 — Antwort-Flow + ACK-Handshake + Resume (Interaction 2.0, §3.3).

Covers four brief scenarios:
  (a) User answers a *blocking* question → task waiting→in_progress, the
      question's awaiting flag clears, a "▶ Antwort erhalten" system line is
      posted, and the answer lands in the assigned agent's poll new_messages.
  (b) User answers a *non-blocking* question → task status untouched.
  (c) First inbound agent Message on a dispatched task claims it (ack_at set)
      through the shared apply_ack_handshake — the Message channel, not comments.
  (d) A second agent Message does NOT re-ACK (ack_at unchanged).
"""
import datetime as dt
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.thread import Message, Thread
from app.models.user import User
from app.services.messaging import ensure_task_thread, post_message

from .conftest import test_engine


@pytest.fixture(autouse=True)
def _enable_comm_v2(monkeypatch):
    """Task 11 adds Agent.comm_v2; expose it so the poll delivery gate resolves True."""
    monkeypatch.setattr(Agent, "comm_v2", True, raising=False)


async def _board(session: AsyncSession) -> Board:
    board = Board(id=uuid.uuid4(), name="AF Board", slug=f"af-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def _agent(board_id: uuid.UUID, current_task_id: uuid.UUID | None = None):
    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Cody-{uuid.uuid4().hex[:4]}",
            role="developer",
            board_id=board_id,
            scopes=["chat:write"],
            provision_status="provisioned",
            agent_token_hash=token_hash,
            current_task_id=current_task_id,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
    return agent, raw_token


async def _task(board_id: uuid.UUID, **kwargs) -> Task:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(id=uuid.uuid4(), board_id=board_id, title="AF Task", **kwargs)
        s.add(task)
        await s.commit()
        await s.refresh(task)
    return task


async def _user_token() -> str:
    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email=f"u-{user_id.hex[:6]}@mc.local", name="Op", role="admin", is_active=True))
        await s.commit()
    return create_access_token(str(user_id), "admin")


async def _post_question(
    task: Task, agent_id: uuid.UUID, *, awaiting: bool = True, blocking: bool = True,
    body: str = "Redis oder Postgres?",
) -> tuple[Thread, Message]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        db_task = await s.get(Task, task.id)
        thread = await ensure_task_thread(s, db_task)
        q = await post_message(
            s,
            thread_id=thread.id,
            sender_type="agent",
            sender_id=agent_id,
            message_type="question",
            body=body,
            question_meta={"awaiting": awaiting, "blocking": blocking, "to": "boss", "priority": "high"},
        )
        return thread, q


@pytest.mark.asyncio
class TestAnswerFlow:
    """POST /api/v1/tasks/{task_id}/thread/messages (user side) + agent Message path."""

    async def test_answer_to_blocking_resumes_task(self, client: AsyncClient):
        """(a) Answer to a blocking question → waiting→in_progress, awaiting cleared,
        system line posted, answer delivered to the assigned agent's poll."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        # Blocking ask parked the task as `waiting`.
        task = await _task(
            board.id, status="waiting", dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=dt.datetime.now(tz=dt.timezone.utc),
        )
        agent, agent_token = await _agent(board.id, current_task_id=task.id)
        # reassign task to that agent
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()
        thread, question = await _post_question(task, agent.id, awaiting=True)

        user_token = await _user_token()
        client.headers["Authorization"] = f"Bearer {user_token}"
        resp = await client.post(
            f"/api/v1/tasks/{task.id}/thread/messages",
            json={"body": "Nimm Postgres.", "reply_to": str(question.id)},
        )
        assert resp.status_code == 201, resp.text

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            resumed = await s.get(Task, task.id)
            assert resumed.status == "in_progress"

            q = await s.get(Message, question.id)
            assert q.question_meta["awaiting"] is False

            msgs = (await s.exec(
                select(Message).where(Message.thread_id == thread.id).order_by(Message.seq)
            )).all()
            system = [m for m in msgs if m.message_type == "system"]
            assert len(system) == 1
            assert "Antwort erhalten" in system[0].body
            assert agent.name in system[0].body

        # Delivery: the answer rides the existing poll path to the assigned agent.
        poll = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert poll.status_code == 200
        bodies = [m["body"] for m in poll.json()["new_messages"]]
        assert "Nimm Postgres." in bodies

    async def test_answering_blocking_resumes_despite_open_non_blocking(self, client: AsyncClient):
        """Resume gate ignores non-blocking questions: a waiting task with one open
        non-blocking AND one blocking question resumes once the BLOCKING one is answered."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(
            board.id, status="waiting", dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=dt.datetime.now(tz=dt.timezone.utc),
        )
        agent, _ = await _agent(board.id, current_task_id=task.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()
        # One non-blocking question stays open the whole time.
        _, non_blocking_q = await _post_question(task, agent.id, awaiting=True, blocking=False, body="FYI ok?")
        thread, blocking_q = await _post_question(task, agent.id, awaiting=True, blocking=True, body="Deploy jetzt?")

        user_token = await _user_token()
        client.headers["Authorization"] = f"Bearer {user_token}"
        resp = await client.post(
            f"/api/v1/tasks/{task.id}/thread/messages",
            json={"body": "Ja, deploy.", "reply_to": str(blocking_q.id)},
        )
        assert resp.status_code == 201, resp.text

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            resumed = await s.get(Task, task.id)
            assert resumed.status == "in_progress"  # resumed despite open non-blocking
            nbq = await s.get(Message, non_blocking_q.id)
            assert nbq.question_meta["awaiting"] is True  # still open, untouched

    async def test_answering_only_non_blocking_does_not_resume(self, client: AsyncClient):
        """A waiting task with an open blocking question does NOT resume when only the
        non-blocking question is answered."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(
            board.id, status="waiting", dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=dt.datetime.now(tz=dt.timezone.utc),
        )
        agent, _ = await _agent(board.id, current_task_id=task.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()
        _, non_blocking_q = await _post_question(task, agent.id, awaiting=True, blocking=False, body="FYI ok?")
        thread, blocking_q = await _post_question(task, agent.id, awaiting=True, blocking=True, body="Deploy jetzt?")

        user_token = await _user_token()
        client.headers["Authorization"] = f"Bearer {user_token}"
        resp = await client.post(
            f"/api/v1/tasks/{task.id}/thread/messages",
            json={"body": "Ja passt.", "reply_to": str(non_blocking_q.id)},
        )
        assert resp.status_code == 201, resp.text

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            still = await s.get(Task, task.id)
            assert still.status == "waiting"  # blocking question still open → no resume
            bq = await s.get(Message, blocking_q.id)
            assert bq.question_meta["awaiting"] is True
            # no resume system line
            systems = (await s.exec(
                select(Message).where(Message.thread_id == thread.id, Message.message_type == "system")
            )).all()
            assert systems == []

    async def test_answer_to_non_blocking_leaves_status(self, client: AsyncClient):
        """(b) Answer to a non-blocking question → task status unchanged (in_progress)."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(board.id, status="in_progress")
        agent, _ = await _agent(board.id, current_task_id=task.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()
        thread, question = await _post_question(task, agent.id, awaiting=True)

        user_token = await _user_token()
        client.headers["Authorization"] = f"Bearer {user_token}"
        resp = await client.post(
            f"/api/v1/tasks/{task.id}/thread/messages",
            json={"body": "Postgres.", "reply_to": str(question.id)},
        )
        assert resp.status_code == 201, resp.text

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            after = await s.get(Task, task.id)
            assert after.status == "in_progress"
            # awaiting still clears — that's the answer semantics
            q = await s.get(Message, question.id)
            assert q.question_meta["awaiting"] is False
            # no system "resume" line for a non-waiting task
            msgs = (await s.exec(
                select(Message).where(Message.thread_id == thread.id, Message.message_type == "system")
            )).all()
            assert msgs == []

    async def test_first_agent_message_acks_task(self, client: AsyncClient):
        """(c) First agent Message on a dispatched (inbox) task → ack_at set,
        status inbox→in_progress via the shared handshake — through the Message path."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(
            board.id, status="inbox", dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=None,
        )
        agent, agent_token = await _agent(board.id, current_task_id=task.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()

        resp = await client.post(
            "/api/v1/agent/tasks/current/messages",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"body": "Fange an."},
        )
        assert resp.status_code == 201, resp.text

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            acked = await s.get(Task, task.id)
            assert acked.ack_at is not None
            assert acked.status == "in_progress"

            msg_id = uuid.UUID(resp.json()["message_id"])
            m = await s.get(Message, msg_id)
            assert m.sender_type == "agent"
            assert m.message_type == "message"
            assert m.body == "Fange an."

    async def test_second_agent_message_does_not_reack(self, client: AsyncClient):
        """(d) Second agent Message does NOT re-ACK — ack_at stays at the first value."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(
            board.id, status="inbox", dispatched_at=dt.datetime.now(tz=dt.timezone.utc),
            ack_at=None,
        )
        agent, agent_token = await _agent(board.id, current_task_id=task.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()

        first = await client.post(
            "/api/v1/agent/tasks/current/messages",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"body": "erste"},
        )
        assert first.status_code == 201
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            ack1 = (await s.get(Task, task.id)).ack_at
        assert ack1 is not None

        second = await client.post(
            "/api/v1/agent/tasks/current/messages",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"body": "zweite"},
        )
        assert second.status_code == 201
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            ack2 = (await s.get(Task, task.id)).ack_at
        assert ack2 == ack1  # no re-ACK

    async def test_agent_message_rejects_question_type(self, client: AsyncClient):
        """Questions go through /ask — the plain message endpoint rejects message_type='question'."""
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = await _board(s)
        task = await _task(board.id, status="in_progress")
        agent, agent_token = await _agent(board.id, current_task_id=task.id)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.assigned_agent_id = agent.id
            s.add(db_task)
            await s.commit()

        resp = await client.post(
            "/api/v1/agent/tasks/current/messages",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"body": "Frage?", "message_type": "question"},
        )
        assert resp.status_code == 422
