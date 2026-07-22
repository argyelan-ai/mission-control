"""Tests for the Nudge+Pull inbox endpoints (W2.1).

`GET /agent/me/inbox` pulls a comm_v2 agent's unread thread messages (the API
call IS the delivery — no cursor advance), and `POST /agent/me/inbox/ack`
advances the per-thread ack cursor (idempotent, never backwards, no cap on
delivered — pull semantics). Both share the scope/cursor/filter core with the
poll delivery path (`_resolve_agent_threads_with_cursors` +
`_unacked_thread_messages`), so the fixtures here mirror test_message_delivery.py.
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.thread import AgentThreadCursor, Message
from app.services.messaging import ensure_task_thread, post_message


async def _board_agent_task(async_session: AsyncSession, *, comm_v2: bool = True, status: str = "in_progress"):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Boss-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        board_id=board.id,
        comm_v2=comm_v2,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Thread probe",
        status=status,
        dispatched_at=now,
        ack_at=now,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    thread = await ensure_task_thread(async_session, task)
    return board, agent, raw_token, task, thread


async def _inbox(client: AsyncClient, token: str):
    return await client.get(
        "/api/v1/agent/me/inbox",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _ack(client: AsyncClient, token: str, thread_id, seq: int):
    return await client.post(
        "/api/v1/agent/me/inbox/ack",
        headers={"Authorization": f"Bearer {token}"},
        json={"thread_id": str(thread_id), "seq": seq},
    )


async def _cursor(async_session: AsyncSession, agent, thread):
    return (
        await async_session.exec(
            select(AgentThreadCursor).where(
                AgentThreadCursor.agent_id == agent.id,
                AgentThreadCursor.thread_id == thread.id,
            )
        )
    ).one()


# ── GET /me/inbox ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inbox_returns_unread_and_max_seq(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    m1 = await post_message(async_session, thread_id=thread.id, sender_type="user",
                            message_type="message", body="one")
    m2 = await post_message(async_session, thread_id=thread.id, sender_type="user",
                            message_type="message", body="two")

    resp = await _inbox(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert [m["id"] for m in body["messages"]] == [str(m1.id), str(m2.id)]
    assert body["messages"][0]["body"] == "one"
    assert body["threads"] == {str(thread.id): m2.seq}


@pytest.mark.asyncio
async def test_inbox_does_not_advance_cursor(client: AsyncClient, async_session):
    """Pure pull-read: GET leaves last_acked/last_delivered untouched — only
    the explicit ack endpoint moves the cursor."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    msg = await post_message(async_session, thread_id=thread.id, sender_type="user",
                             message_type="message", body="hello")

    await _inbox(client, token)
    cur = await _cursor(async_session, agent, thread)
    await async_session.refresh(cur)
    assert cur.last_acked_seq == 0
    assert cur.last_delivered_seq == 0

    # Re-pull returns the same message (nothing consumed without an ack).
    again = await _inbox(client, token)
    assert [m["id"] for m in again.json()["messages"]] == [str(msg.id)]


@pytest.mark.asyncio
async def test_inbox_excludes_own_messages(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    own = await post_message(async_session, thread_id=thread.id, sender_type="agent",
                             sender_id=agent.id, message_type="message", body="mine")
    user_msg = await post_message(async_session, thread_id=thread.id, sender_type="user",
                                  message_type="message", body="theirs")

    body = (await _inbox(client, token)).json()
    ids = [m["id"] for m in body["messages"]]
    assert str(own.id) not in ids
    assert ids == [str(user_msg.id)]
    # max_seq spans own posts too, so a single ack clears the whole window.
    assert body["threads"] == {str(thread.id): own.seq if own.seq > user_msg.seq else user_msg.seq}


@pytest.mark.asyncio
async def test_inbox_empty_when_no_messages(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    body = (await _inbox(client, token)).json()
    assert body == {"messages": [], "threads": {}}


@pytest.mark.asyncio
async def test_inbox_empty_without_comm_v2(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session, comm_v2=False)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="hidden")
    resp = await _inbox(client, token)
    assert resp.status_code == 200
    assert resp.json() == {"messages": [], "threads": {}}


@pytest.mark.asyncio
async def test_inbox_finished_task_fast_forwards(client: AsyncClient, async_session):
    """A done task's thread starts its first cursor at max(seq): historic
    messages are not pulled, only ones posted after first sight (Befund C)."""
    board, agent, token, task, thread = await _board_agent_task(async_session, status="done")
    for i in range(3):
        await post_message(async_session, thread_id=thread.id, sender_type="user",
                           message_type="message", body=f"hist {i}")

    body = (await _inbox(client, token)).json()
    assert body["messages"] == []

    late = await post_message(async_session, thread_id=thread.id, sender_type="user",
                              message_type="message", body="after")
    body2 = (await _inbox(client, token)).json()
    assert [m["id"] for m in body2["messages"]] == [str(late.id)]


# ── POST /me/inbox/ack ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ack_advances_and_hides(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    m1 = await post_message(async_session, thread_id=thread.id, sender_type="user",
                            message_type="message", body="one")
    m2 = await post_message(async_session, thread_id=thread.id, sender_type="user",
                            message_type="message", body="two")

    resp = await _ack(client, token, thread.id, m2.seq)
    assert resp.status_code == 200
    j = resp.json()
    assert j["last_acked_seq"] == m2.seq
    assert j["last_delivered_seq"] == m2.seq  # dragged up (delivered >= acked)

    # Both messages now cleared from the inbox.
    assert (await _inbox(client, token)).json()["messages"] == []


@pytest.mark.asyncio
async def test_ack_is_idempotent_and_never_backwards(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    for _ in range(5):
        await post_message(async_session, thread_id=thread.id, sender_type="user",
                           message_type="message", body="m")

    await _ack(client, token, thread.id, 5)
    # A lower ack must not roll the cursor back.
    resp = await _ack(client, token, thread.id, 2)
    assert resp.json()["last_acked_seq"] == 5
    # Re-acking the same seq is a no-op success.
    resp2 = await _ack(client, token, thread.id, 5)
    assert resp2.json()["last_acked_seq"] == 5

    cur = await _cursor(async_session, agent, thread)
    await async_session.refresh(cur)
    assert cur.last_acked_seq == 5


@pytest.mark.asyncio
async def test_ack_no_cap_on_delivered(client: AsyncClient, async_session):
    """Pull semantics: unlike the poll acked_seq path, the ack is NOT capped at
    last_delivered_seq — the GET was the delivery. last_delivered rises to meet
    it so the two-stage invariant holds."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="one")

    # Ack seq 1 without ever having a delivered cursor advance.
    resp = await _ack(client, token, thread.id, 1)
    j = resp.json()
    assert j["last_acked_seq"] == 1
    assert j["last_delivered_seq"] == 1

    cur = await _cursor(async_session, agent, thread)
    await async_session.refresh(cur)
    assert cur.last_delivered_seq >= cur.last_acked_seq


@pytest.mark.asyncio
async def test_ack_404_without_comm_v2(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session, comm_v2=False)
    resp = await _ack(client, token, thread.id, 1)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ack_capped_at_thread_max_seq(client: AsyncClient, async_session):
    """Finding 3a: an ack above the thread's highest real seq is capped — it
    must NOT set the cursor past messages that don't exist yet (which would
    permanently skip them)."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    m1 = await post_message(async_session, thread_id=thread.id, sender_type="user",
                            message_type="message", body="one")

    resp = await _ack(client, token, thread.id, 999)
    assert resp.status_code == 200
    assert resp.json()["last_acked_seq"] == m1.seq  # capped at 1, not 999

    # A later message (seq 2) is still delivered — not skipped.
    m2 = await post_message(async_session, thread_id=thread.id, sender_type="user",
                            message_type="message", body="two")
    ids = [m["id"] for m in (await _inbox(client, token)).json()["messages"]]
    assert str(m2.id) in ids


@pytest.mark.asyncio
async def test_ack_foreign_thread_404_and_no_cursor(client: AsyncClient, async_session):
    """Finding 3b: acking a thread outside the agent's active scope returns 404
    and creates NO cursor row (no pre-poisoning / table bloat)."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    bogus = uuid.uuid4()

    resp = await _ack(client, token, bogus, 5)
    assert resp.status_code == 404

    cursors = (
        await async_session.exec(
            select(AgentThreadCursor).where(
                AgentThreadCursor.agent_id == agent.id,
                AgentThreadCursor.thread_id == bogus,
            )
        )
    ).all()
    assert cursors == [], "no cursor may be created for a foreign thread"


@pytest.mark.asyncio
async def test_ack_other_agents_thread_404(client: AsyncClient, async_session):
    """A real thread that belongs to a DIFFERENT agent is still out of scope —
    404, no cursor created for the caller."""
    _, _, _, _, other_thread = await _board_agent_task(async_session)
    board, agent, token, task, thread = await _board_agent_task(async_session)

    resp = await _ack(client, token, other_thread.id, 3)
    assert resp.status_code == 404

    cursors = (
        await async_session.exec(
            select(AgentThreadCursor).where(
                AgentThreadCursor.agent_id == agent.id,
                AgentThreadCursor.thread_id == other_thread.id,
            )
        )
    ).all()
    assert cursors == []
