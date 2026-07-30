"""An agent must be able to answer where it was spoken to.

Live finding 2026-07-29 (Slack team chat, thread 8015c75e): Mark wrote to Boss
in the general chat, MC delivered it and Boss acked it within ten seconds
(`last_acked_seq=5`) — and Boss never replied, because he could not. The only
write path an agent had was `POST /tasks/current/messages`, bound to
`agent.current_task_id`: 409 without an active task, and with one it would have
written into the *task* thread instead of the conversation that asked. Mark saw
an answer arrive over Telegram only because Boss fell back to the old board
chat, the single route still open to him.

These tests pin the missing direction: reply into a named thread, authorised by
the same scope rule that governs delivery, mirrored into the chat channels like
any other thread message.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.task import Task
from app.models.thread import Message, Thread
from tests.conftest import test_engine


async def _agent(session: AsyncSession, name: str = "Boss") -> tuple[Agent, str]:
    raw, token_hash = generate_agent_token()
    agent = Agent(
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        comm_v2=True,
        scopes=["chat:write", "tasks:read", "tasks:write"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _messages(thread_id: uuid.UUID) -> list[Message]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        res = await s.exec(
            select(Message).where(Message.thread_id == thread_id).order_by(Message.seq)
        )
        return list(res.all())


# ── The gap that was measured live ────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_replies_in_its_dm_thread_without_any_task(client: AsyncClient):
    """The exact live scenario: no active task, message in the general chat.

    Boss must be able to answer. Before this endpoint the only write path
    returned 409 here.
    """
    from app.services.messaging import ensure_dm_thread, post_message

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        thread = await ensure_dm_thread(s, agent)
        await post_message(
            s, thread_id=thread.id, sender_type="user",
            message_type="message", body="hey boss alles klar?",
            mirror_to_telegram=False,
        )
        assert agent.current_task_id is None, "fixture must reproduce the live state"

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "Alles klar, Mark — laeuft."},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    msgs = await _messages(thread.id)
    assert [m.sender_type for m in msgs] == ["user", "agent"], (
        "the agent's reply must land in the very thread that asked"
    )
    assert msgs[-1].sender_id == agent.id


@pytest.mark.asyncio
async def test_the_reply_is_mirrored_into_the_chat_channels(client: AsyncClient):
    """The point of the whole exercise: the answer reaches Mark's chat.

    `post_message` mirrors into every active channel, so a reply posted this way
    goes back out over Slack (and Telegram) without this endpoint knowing either
    channel exists.
    """
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        thread = await ensure_dm_thread(s, agent)

    with patch(
        "app.services.chat_outbound.mirror_message_to_all", new_callable=AsyncMock
    ) as mirror:
        resp = await client.post(
            f"/api/v1/agent/threads/{thread.id}/messages",
            json={"body": "Antwort an Mark"},
            headers=_auth(token),
        )

    assert resp.status_code == 201, resp.text
    assert mirror.await_count == 1, "reply was not handed to the chat mirror"
    mirrored = mirror.await_args.args[1]
    assert mirrored.body == "Antwort an Mark"


@pytest.mark.asyncio
async def test_agent_replies_in_its_active_task_thread(client: AsyncClient):
    """The task conversation keeps working through the same door."""
    from app.models.board import Board
    from app.services.messaging import ensure_task_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        task = Task(
            board_id=board.id, title="T", status="in_progress",
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        thread = await ensure_task_thread(s, task)

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "Zwischenstand", "message_type": "status"},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert (await _messages(thread.id))[-1].message_type == "status"


# ── The door is exactly as wide as the delivery scope ─────────────────────
#
# Every refusal test below also posts once into a thread the agent DOES own and
# demands a 201. Without that control the whole group passes while the endpoint
# does not exist at all — an absent route answers 404 too, and three "green"
# tests would be asserting nothing. (This bit us on 2026-07-29 in the drift
# watchdog tests; same trap, different file.)


async def _control_post_succeeds(client: AsyncClient, token: str, thread_id) -> None:
    resp = await client.post(
        f"/api/v1/agent/threads/{thread_id}/messages",
        json={"body": "Kontrolle: dieser Weg muss offen sein"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, (
        "the control post failed — the refusal above proves nothing, because a "
        f"missing route would also answer 404. Got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_a_foreign_dm_thread_is_refused(client: AsyncClient):
    """Rex must not be able to write into Boss's conversation with Mark."""
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        boss, _boss_token = await _agent(s, "Boss")
        rex, rex_token = await _agent(s, "Rex")
        boss_thread = await ensure_dm_thread(s, boss)
        rex_thread = await ensure_dm_thread(s, rex)

    resp = await client.post(
        f"/api/v1/agent/threads/{boss_thread.id}/messages",
        json={"body": "ich bin nicht Boss"},
        headers=_auth(rex_token),
    )

    assert resp.status_code == 404, resp.text
    assert await _messages(boss_thread.id) == []
    await _control_post_succeeds(client, rex_token, rex_thread.id)


@pytest.mark.asyncio
async def test_a_thread_of_someone_elses_task_is_refused(client: AsyncClient):
    """Same rule for task threads — assignment decides, not existence."""
    from app.models.board import Board
    from app.services.messaging import ensure_dm_thread, ensure_task_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        owner, _ = await _agent(s, "Owner")
        stranger, stranger_token = await _agent(s, "Stranger")
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        task = Task(
            board_id=board.id, title="T", status="in_progress",
            assigned_agent_id=owner.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        thread = await ensure_task_thread(s, task)
        stranger_thread = await ensure_dm_thread(s, stranger)

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "fremde Aufgabe"},
        headers=_auth(stranger_token),
    )

    assert resp.status_code == 404, resp.text
    await _control_post_succeeds(client, stranger_token, stranger_thread.id)


@pytest.mark.asyncio
async def test_an_unknown_thread_is_refused_the_same_way(client: AsyncClient):
    """404, indistinguishable from "not yours" — no probing for conversations."""
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent_row, token = await _agent(s)
        own_thread = await ensure_dm_thread(s, agent_row)

    resp = await client.post(
        f"/api/v1/agent/threads/{uuid.uuid4()}/messages",
        json={"body": "gibt es nicht"},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text
    await _control_post_succeeds(client, token, own_thread.id)


@pytest.mark.asyncio
async def test_questions_still_go_through_ask(client: AsyncClient):
    """`question` carries awaiting semantics and stays on /tasks/current/ask."""
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        thread = await ensure_dm_thread(s, agent)

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "Darf ich?", "message_type": "question"},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_chat_write_scope_is_required(client: AsyncClient):
    """An agent without chat:write cannot use the new door."""
    from app.services.messaging import ensure_dm_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        raw, token_hash = generate_agent_token()
        agent = Agent(
            name=f"Mute-{uuid.uuid4().hex[:6]}",
            agent_runtime="host",
            agent_token_hash=token_hash,
            comm_v2=True,
            scopes=["tasks:read"],
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        thread = await ensure_dm_thread(s, agent)

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "darf ich nicht"},
        headers=_auth(raw),
    )
    assert resp.status_code == 403, resp.text


# ── The old verb keeps working, without an active task ────────────────────
#
# The new endpoint fixes the *contract*, but every agent alive already types
# `mc msg "…"`, which posts to /tasks/current/messages. Making them all relearn
# a flag would mean touching 13 agent cards for a bug that is ours. So the old
# door stops slamming: with no active task the agent is plainly having a
# conversation, not filing a work note, and the DM thread is where that belongs.


@pytest.mark.asyncio
async def test_msg_without_an_active_task_lands_in_the_dm_thread(client: AsyncClient):
    """The live scenario, through the verb agents actually use today."""
    from app.services.messaging import ensure_dm_thread, post_message

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        thread = await ensure_dm_thread(s, agent)
        await post_message(
            s, thread_id=thread.id, sender_type="user",
            message_type="message", body="hey boss alles klar?",
            mirror_to_telegram=False,
        )

    resp = await client.post(
        "/api/v1/agent/tasks/current/messages",
        json={"body": "Alles klar, Mark."},
        headers=_auth(token),
    )

    assert resp.status_code == 201, (
        f"no active task must no longer be a dead end — got {resp.status_code}"
    )
    assert resp.json()["thread_id"] == str(thread.id)
    msgs = await _messages(thread.id)
    assert [m.sender_type for m in msgs] == ["user", "agent"]


@pytest.mark.asyncio
async def test_msg_still_prefers_the_active_task_thread(client: AsyncClient):
    """With a task in hand, a work note still belongs to the task — the DM
    fallback must not hijack it."""
    from app.models.board import Board
    from app.services.messaging import ensure_dm_thread, ensure_task_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        dm = await ensure_dm_thread(s, agent)
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        task = Task(
            board_id=board.id, title="T", status="in_progress",
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        task_thread = await ensure_task_thread(s, task)
        agent.current_task_id = task.id
        s.add(agent)
        await s.commit()

    resp = await client.post(
        "/api/v1/agent/tasks/current/messages",
        json={"body": "Zwischenstand", "message_type": "status"},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["thread_id"] == str(task_thread.id)
    assert await _messages(dm.id) == [], "the DM must stay untouched"


@pytest.mark.asyncio
async def test_an_agent_with_neither_task_nor_dm_still_gets_a_clear_refusal(
    client: AsyncClient,
):
    """No task and no conversation to fall back on: 409 as before, so the
    fallback cannot silently invent a thread out of nothing."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _agent_row, token = await _agent(s)

    resp = await client.post(
        "/api/v1/agent/tasks/current/messages",
        json={"body": "ins Leere"},
        headers=_auth(token),
    )
    assert resp.status_code == 409, resp.text


# ── Review findings 2026-07-30 (adversarial pass on this very branch) ─────


@pytest.mark.asyncio
async def test_a_reply_on_a_task_thread_acks_the_dispatch(client: AsyncClient):
    """Finding 1: the new door must not be weaker than the old one.

    `/tasks/current/messages` claims a dispatched task via the ACK handshake.
    Without the same step here, an agent that answers through `--thread` looks
    unresponsive and the task monitor reassigns it after ten minutes.
    """
    from app.models.board import Board
    from app.services.messaging import ensure_task_thread
    from app.utils import utcnow

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        task = Task(
            board_id=board.id, title="T", status="inbox",
            assigned_agent_id=agent.id, dispatched_at=utcnow(),
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        thread = await ensure_task_thread(s, task)

    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "verstanden, ich fange an"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.ack_at is not None, (
            "answering on a task thread must ACK the dispatch — otherwise the "
            "watchdog reassigns a task the agent is already working on"
        )


@pytest.mark.asyncio
async def test_reply_to_cannot_reach_into_another_thread(client: AsyncClient):
    """Finding 2: clearing `awaiting` across threads strands a waiting task.

    A blocking question lives on a task thread; answering it is what resumes
    the task. If a message in the DM thread may clear that flag, the task keeps
    status `waiting` with no open question — and nothing ever resumes it.
    """
    from app.models.board import Board
    from app.services.messaging import (
        ensure_dm_thread, ensure_task_thread, post_message,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        dm = await ensure_dm_thread(s, agent)
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        task = Task(
            board_id=board.id, title="T", status="waiting",
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        task_thread = await ensure_task_thread(s, task)
        question = await post_message(
            s, thread_id=task_thread.id, sender_type="agent", sender_id=agent.id,
            message_type="question", body="Postgres oder SQLite?",
            question_meta={"awaiting": True, "to": "mark", "priority": "high"},
            mirror_to_telegram=False,
        )

    resp = await client.post(
        f"/api/v1/agent/threads/{dm.id}/messages",
        json={"body": "nebenbei", "reply_to": str(question.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 422, (
        f"a reply must stay inside its own thread — got {resp.status_code}"
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        still = await s.get(Message, question.id)
        assert still.question_meta.get("awaiting") is True, (
            "the blocking question was cleared from a foreign thread"
        )


@pytest.mark.asyncio
async def test_msg_uses_the_task_thread_even_after_current_task_was_released(
    client: AsyncClient,
):
    """Finding 4: `current_task_id` is released while the task still belongs
    to the agent (blocked, review). A follow-up note must not slip silently
    into the general chat, where the task conversation is not being read.
    """
    from app.models.board import Board
    from app.services.messaging import ensure_dm_thread, ensure_task_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        dm = await ensure_dm_thread(s, agent)
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        task = Task(
            board_id=board.id, title="T", status="review",
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        task_thread = await ensure_task_thread(s, task)
        assert agent.current_task_id is None  # released on leaving in_progress

    resp = await client.post(
        "/api/v1/agent/tasks/current/messages",
        json={"body": "Nachtrag zum Review"},
        headers=_auth(token),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["thread_id"] == str(task_thread.id), (
        "the note belongs to the task conversation, not the general chat"
    )
    assert await _messages(dm.id) == []


@pytest.mark.asyncio
async def test_msg_is_refused_when_several_tasks_could_be_meant(
    client: AsyncClient,
):
    """Ambiguity must be loud, not guessed: two open tasks, no current one."""
    from app.models.board import Board
    from app.services.messaging import ensure_task_thread

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _agent(s)
        board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        for _ in range(2):
            task = Task(
                board_id=board.id, title="T", status="review",
                assigned_agent_id=agent.id,
            )
            s.add(task)
            await s.commit()
            await s.refresh(task)
            await ensure_task_thread(s, task)

    resp = await client.post(
        "/api/v1/agent/tasks/current/messages",
        json={"body": "welcher denn?"},
        headers=_auth(token),
    )
    assert resp.status_code == 409, resp.text
    assert "--thread" in resp.json()["detail"], (
        "an ambiguous refusal must name the way out"
    )


# ── Delivery and reply must never drift apart ─────────────────────────────


@pytest.mark.asyncio
async def test_delivery_and_reply_read_the_same_scope_rule(async_session: AsyncSession):
    """Structural guard, not a behaviour test.

    The two directions were asymmetric once and that asymmetry cost a day of
    silent one-way conversation. If someone re-implements either side against
    its own rule, this fails.
    """
    from app.routers import agents as agents_router
    from app.routers import agent_scoped
    from app.services import thread_scope

    assert (
        agents_router._message_threads_for_agent is thread_scope.message_threads_for_agent
    ), "the delivery path no longer uses the shared scope rule"
    assert (
        agent_scoped.thread_agent_may_write_to is thread_scope.thread_agent_may_write_to
    ), "the reply path no longer uses the shared scope rule"
