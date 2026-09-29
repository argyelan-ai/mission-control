"""A task parked in `waiting` by a blocking question must be answerable from
the task detail (journey J-phone-needs-you, 2026-09-29).

Two ways an operator answer can arrive:
  1. The "Reply" button — a THREAD reply with `reply_to` = the open question.
     The UI needs the open question to address it: GET /tasks/{id}/thread
     carries `open_question`.
  2. A plain operator comment typed into the comment box while the task
     waits (backstop). It counts as the answer only when exactly ONE blocking
     question is open — then the question clears and the task resumes. The
     answer reaches the agent exactly once, via the comment channel; no
     thread copy is written.
"""
import datetime as dt
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.thread import Message
from app.models.user import User
from app.services.messaging import ensure_task_thread, post_message

from .conftest import test_engine


async def _setup_waiting_task(*, comm_v2: bool = True):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Reply Board", slug=f"rp-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        now = dt.datetime.now(tz=dt.timezone.utc)
        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="Reply Task",
            status="waiting", dispatched_at=now, ack_at=now,
        )
        s.add(task)
        await s.commit()
        raw_token, token_hash = generate_agent_token()
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Lead-{uuid.uuid4().hex[:4]}",
            role="developer",
            board_id=board.id,
            scopes=["chat:write"],
            provision_status="provisioned",
            agent_token_hash=token_hash,
            current_task_id=task.id,
            comm_v2=comm_v2,
        )
        s.add(agent)
        await s.commit()
        task.assigned_agent_id = agent.id
        s.add(task)
        await s.commit()
    return board, task, agent, raw_token


async def _ask(task: Task, agent_id: uuid.UUID, body: str, *, blocking: bool = True, options=None) -> Message:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        db_task = await s.get(Task, task.id)
        thread = await ensure_task_thread(s, db_task)
        return await post_message(
            s,
            thread_id=thread.id,
            sender_type="agent",
            sender_id=agent_id,
            message_type="question",
            body=body,
            question_meta={
                "awaiting": True, "blocking": blocking, "to": "boss",
                "priority": "high", "options": options,
            },
        )


async def _user_headers() -> dict:
    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email=f"u-{user_id.hex[:6]}@mc.local", name="Op", role="admin", is_active=True))
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(str(user_id), 'admin')}"}


class _Poller:
    """Polls like the real bridge: acks every delivered thread seq on the next
    poll (the thread channel is at-least-once until acked)."""

    def __init__(self, client: AsyncClient, agent_token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {agent_token}"}
        self.acked: dict[str, int] = {}

    async def texts(self) -> list[str]:
        """Every text the agent received in one poll, across BOTH channels."""
        params = {"acked_seq": json.dumps(self.acked)} if self.acked else {}
        resp = await self.client.get("/api/v1/agent/me/poll", headers=self.headers, params=params)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        texts = [c["content"] for c in data.get("new_comments", [])]
        for m in data.get("new_messages", []):
            texts.append(m["body"])
            self.acked[m["thread_id"]] = max(self.acked.get(m["thread_id"], 0), m["seq"])
        return texts


async def _status(task_id: uuid.UUID) -> str:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.get(Task, task_id)).status


async def _awaiting(question_id: uuid.UUID) -> bool:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return bool((await s.get(Message, question_id)).question_meta.get("awaiting"))


@pytest.mark.asyncio
class TestOpenQuestionOnThread:
    async def test_thread_read_names_the_open_blocking_question(self, client: AsyncClient):
        _, task, agent, _ = await _setup_waiting_task()
        q = await _ask(task, agent.id, "Aurora or Borealis?", options=["Aurora", "Borealis"])
        # A newer NON-blocking question must not hide the one the task waits on.
        await _ask(task, agent.id, "FYI: logs rotated?", blocking=False)

        resp = await client.get(f"/api/v1/tasks/{task.id}/thread", headers=await _user_headers())
        assert resp.status_code == 200, resp.text
        open_q = resp.json()["open_question"]
        assert open_q["id"] == str(q.id)
        assert open_q["body"] == "Aurora or Borealis?"
        assert open_q["blocking"] is True
        assert open_q["options"] == ["Aurora", "Borealis"]
        assert open_q["author"]["display"] == agent.name
        assert open_q["asker_agent_id"] == str(agent.id)

    async def test_thread_read_open_question_is_null_once_answered(self, client: AsyncClient):
        _, task, agent, _ = await _setup_waiting_task()
        q = await _ask(task, agent.id, "Aurora or Borealis?")
        headers = await _user_headers()
        resp = await client.post(
            f"/api/v1/tasks/{task.id}/thread/messages",
            json={"body": "Aurora.", "reply_to": str(q.id)}, headers=headers,
        )
        assert resp.status_code == 201, resp.text

        resp = await client.get(f"/api/v1/tasks/{task.id}/thread", headers=headers)
        assert resp.json()["open_question"] is None

    async def test_task_without_thread_has_no_open_question(self, client: AsyncClient):
        _, task, _, _ = await _setup_waiting_task()
        resp = await client.get(f"/api/v1/tasks/{task.id}/thread", headers=await _user_headers())
        assert resp.json()["open_question"] is None


@pytest.mark.asyncio
class TestReplyReachesAgentOnce:
    async def test_thread_reply_resumes_and_is_delivered_exactly_once(self, client: AsyncClient):
        _, task, agent, agent_token = await _setup_waiting_task()
        q = await _ask(task, agent.id, "Aurora or Borealis?")

        resp = await client.post(
            f"/api/v1/tasks/{task.id}/thread/messages",
            json={"body": "Aurora.", "reply_to": str(q.id)}, headers=await _user_headers(),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["task_status"] == "in_progress"

        poller = _Poller(client, agent_token)
        first = await poller.texts()
        second = await poller.texts()
        assert (first + second).count("Aurora.") == 1


@pytest.mark.asyncio
class TestCommentBackstop:
    async def _comment(self, client, board, task, content, **extra):
        return await client.post(
            f"/api/v1/boards/{board.id}/tasks/{task.id}/comments",
            json={"content": content, **extra}, headers=await _user_headers(),
        )

    async def test_operator_comment_answers_the_single_open_question(self, client: AsyncClient):
        board, task, agent, agent_token = await _setup_waiting_task()
        q = await _ask(task, agent.id, "Aurora or Borealis?")

        resp = await self._comment(client, board, task, "Aurora.", comment_type="progress")
        assert resp.status_code == 201, resp.text

        assert await _status(task.id) == "in_progress"
        assert await _awaiting(q.id) is False
        poller = _Poller(client, agent_token)
        first = await poller.texts()
        second = await poller.texts()
        # Exactly once — the comment channel carries it, no thread copy.
        assert (first + second).count("Aurora.") == 1

    async def test_comment_does_not_guess_between_two_open_questions(self, client: AsyncClient):
        board, task, agent, _ = await _setup_waiting_task()
        q1 = await _ask(task, agent.id, "Name?")
        q2 = await _ask(task, agent.id, "Date?")

        resp = await self._comment(client, board, task, "Aurora.")
        assert resp.status_code == 201, resp.text

        assert await _status(task.id) == "waiting"
        assert await _awaiting(q1.id) is True
        assert await _awaiting(q2.id) is True

    async def test_non_blocking_question_alone_is_not_answered_by_comment(self, client: AsyncClient):
        board, task, agent, _ = await _setup_waiting_task()
        q = await _ask(task, agent.id, "FYI?", blocking=False)

        resp = await self._comment(client, board, task, "ok")
        assert resp.status_code == 201, resp.text

        assert await _status(task.id) == "waiting"
        assert await _awaiting(q.id) is True

    async def test_agent_comment_is_not_an_answer(self, client: AsyncClient):
        board, task, agent, _ = await _setup_waiting_task()
        q = await _ask(task, agent.id, "Aurora or Borealis?")

        resp = await self._comment(
            client, board, task, "still waiting",
            author_type="agent", author_agent_id=str(agent.id),
        )
        assert resp.status_code == 201, resp.text

        assert await _status(task.id) == "waiting"
        assert await _awaiting(q.id) is True

    async def test_comment_on_running_task_leaves_questions_alone(self, client: AsyncClient):
        board, task, agent, _ = await _setup_waiting_task()
        q = await _ask(task, agent.id, "Aurora or Borealis?", blocking=False)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.status = "in_progress"
            s.add(db_task)
            await s.commit()

        resp = await self._comment(client, board, task, "Aurora.")
        assert resp.status_code == 201, resp.text
        assert await _awaiting(q.id) is True
