"""Stale messages must not wake an agent (inbox nudges, live finding 2026-10-09).

poll.sh / the omp bridge paste "📬 Neue Nachrichten … mc inbox" whenever
`/me/poll` returns a non-empty `new_messages`, and re-paste every ten minutes
until `mc inbox` acks. The lead's turn gate stayed closed for a week; the
moment it opened, two week-old "✅ TASK ERLEDIGT" system notices in its DM
thread woke it, and it went off inspecting a benchmark card that had been
approved days before.

The rule these tests pin down (``routers/agents._is_stale_message``):

* operator messages never expire — the operator's word is always delivered;
* system notices expire after ``agent_message_stale_after_seconds``;
* peer-agent messages expire after the same age only when nobody works the
  thread any more (its task is done/failed/aborted or the thread is closed);
* fresh messages are delivered everywhere, exactly as before.

An expired message is skipped like a briefing or an own post, and the cursor
is caught up past any leading run of messages that can never be delivered, so
the backlog clears itself server-side instead of lingering as "unacked".
"""
import datetime as dt
import json
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
from app.services.messaging import (
    create_chat_thread,
    ensure_dm_thread,
    ensure_task_thread,
    post_message,
)

WEEK_AGO = dt.timedelta(days=7)


async def _board(async_session: AsyncSession) -> Board:
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)
    return board


async def _agent(async_session: AsyncSession, board: Board, *, lead: bool = False):
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Agent-{uuid.uuid4().hex[:6]}",
        slug=f"agent-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        board_id=board.id,
        comm_v2=True,
        is_board_lead=lead,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    return agent, raw_token


async def _task(async_session: AsyncSession, board: Board, agent: Agent, status: str) -> Task:
    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Card",
        status=status,
        dispatched_at=now,
        ack_at=now,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    return task


async def _post(async_session, thread, *, sender_type="user", sender_id=None,
                message_type="message", body="hi", age: dt.timedelta | None = None,
                question_meta=None) -> Message:
    msg = await post_message(
        async_session, thread_id=thread.id, sender_type=sender_type,
        sender_id=sender_id, message_type=message_type, body=body,
        question_meta=question_meta,
    )
    if age is not None:
        msg.created_at = dt.datetime.now(tz=dt.timezone.utc) - age
        async_session.add(msg)
        await async_session.commit()
        await async_session.refresh(msg)
    return msg


async def _seen_up_to(async_session, agent, thread, seq: int) -> None:
    """A cursor that already acked `seq` — the agent had read the thread up
    to there before the stale notices arrived (the live DM shape)."""
    async_session.add(AgentThreadCursor(
        agent_id=agent.id, thread_id=thread.id,
        last_delivered_seq=seq, last_acked_seq=seq,
    ))
    await async_session.commit()


async def _poll_ids(client: AsyncClient, token: str) -> list[str]:
    resp = await client.get(
        "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return [m["id"] for m in resp.json().get("new_messages", [])]


async def _inbox(client: AsyncClient, token: str) -> dict:
    resp = await client.get(
        "/api/v1/agent/me/inbox", headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json()


async def _cursor(async_session, agent, thread) -> AgentThreadCursor:
    cur = (await async_session.exec(
        select(AgentThreadCursor).where(
            AgentThreadCursor.agent_id == agent.id,
            AgentThreadCursor.thread_id == thread.id,
        )
    )).one()
    await async_session.refresh(cur)
    return cur


# ── DM thread: the live incident ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_week_old_system_notice_in_dm_does_not_nudge(client: AsyncClient, async_session):
    """The live shape: a lead read its DM up to seq 1, then two completion
    notices landed and sat there for a week behind a closed turn gate."""
    board = await _board(async_session)
    lead, token = await _agent(async_session, board, lead=True)
    dm = await ensure_dm_thread(async_session, lead)
    first = await _post(async_session, dm, body="earlier operator note", age=WEEK_AGO * 2)
    await _seen_up_to(async_session, lead, dm, first.seq)
    await _post(async_session, dm, sender_type="system", message_type="system",
                body="# ✅ TASK ERLEDIGT: bench run 1", age=WEEK_AGO + dt.timedelta(days=1))
    notice2 = await _post(async_session, dm, sender_type="system", message_type="system",
                          body="# ✅ TASK ERLEDIGT: bench run 2", age=WEEK_AGO)

    assert await _poll_ids(client, token) == []
    assert (await _inbox(client, token))["messages"] == []
    # Caught up server-side — no phantom backlog, no re-scan on every poll.
    cur = await _cursor(async_session, lead, dm)
    assert cur.last_acked_seq == notice2.seq
    assert cur.last_delivered_seq == notice2.seq


@pytest.mark.asyncio
async def test_fresh_system_notice_in_dm_still_nudges(client: AsyncClient, async_session):
    board = await _board(async_session)
    lead, token = await _agent(async_session, board, lead=True)
    dm = await ensure_dm_thread(async_session, lead)
    notice = await _post(async_session, dm, sender_type="system", message_type="system",
                         body="# ✅ TASK ERLEDIGT: fresh card")

    assert await _poll_ids(client, token) == [str(notice.id)]
    assert [m["id"] for m in (await _inbox(client, token))["messages"]] == [str(notice.id)]


@pytest.mark.asyncio
async def test_old_operator_message_in_dm_still_nudges(client: AsyncClient, async_session):
    """The operator's word never expires, however long the gate was closed."""
    board = await _board(async_session)
    lead, token = await _agent(async_session, board, lead=True)
    dm = await ensure_dm_thread(async_session, lead)
    note = await _post(async_session, dm, body="please look at this", age=WEEK_AGO)

    assert await _poll_ids(client, token) == [str(note.id)]


@pytest.mark.asyncio
async def test_catch_up_stops_at_the_first_deliverable_message(client: AsyncClient, async_session):
    """A stale notice BEFORE an unread operator message: the cursor may only
    skip up to the notice — jumping past the operator message would mark it
    read without anyone having seen it."""
    board = await _board(async_session)
    lead, token = await _agent(async_session, board, lead=True)
    dm = await ensure_dm_thread(async_session, lead)
    stale = await _post(async_session, dm, sender_type="system", message_type="system",
                        body="old notice", age=WEEK_AGO)
    note = await _post(async_session, dm, body="still unread", age=WEEK_AGO)
    later_stale = await _post(async_session, dm, sender_type="system", message_type="system",
                              body="another old notice", age=WEEK_AGO)

    body = await _inbox(client, token)
    assert [m["id"] for m in body["messages"]] == [str(note.id)]
    # Ack target still spans the whole window, so `mc inbox` clears it in one go.
    assert body["threads"] == {str(dm.id): later_stale.seq}
    cur = await _cursor(async_session, lead, dm)
    assert cur.last_acked_seq == stale.seq


# ── Task threads ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_done_task_thread_old_peer_reply_does_not_nudge(client: AsyncClient, async_session):
    """Late chatter on a card nobody works any more ("the card is long closed,
    nobody needs to follow up") must not wake its former assignee."""
    board = await _board(async_session)
    agent, token = await _agent(async_session, board)
    peer, _ = await _agent(async_session, board)
    task = await _task(async_session, board, agent, "in_progress")
    thread = await ensure_task_thread(async_session, task)
    await _seen_up_to(async_session, agent, thread, 0)
    reply = await _post(async_session, thread, sender_type="agent", sender_id=peer.id,
                        body="late reply", age=dt.timedelta(days=3))
    task.status = "done"
    async_session.add(task)
    await async_session.commit()

    assert await _poll_ids(client, token) == []
    cur = await _cursor(async_session, agent, thread)
    assert cur.last_acked_seq == reply.seq


@pytest.mark.asyncio
async def test_done_task_thread_fresh_operator_follow_up_still_nudges(client: AsyncClient, async_session):
    """A finished card stays reachable: a fresh operator follow-up (or a fresh
    peer post) in its thread is delivered as before."""
    board = await _board(async_session)
    agent, token = await _agent(async_session, board)
    task = await _task(async_session, board, agent, "in_progress")
    thread = await ensure_task_thread(async_session, task)
    await _seen_up_to(async_session, agent, thread, 0)
    task.status = "done"
    async_session.add(task)
    await async_session.commit()
    follow_up = await _post(async_session, thread, body="one more thing")

    assert await _poll_ids(client, token) == [str(follow_up.id)]


@pytest.mark.asyncio
async def test_active_task_thread_old_peer_message_still_nudges(client: AsyncClient, async_session):
    """While the card is being worked, a peer message is delivered no matter
    how long it waited — only finished threads let peer chatter expire."""
    board = await _board(async_session)
    agent, token = await _agent(async_session, board)
    peer, _ = await _agent(async_session, board)
    task = await _task(async_session, board, agent, "in_progress")
    thread = await ensure_task_thread(async_session, task)
    msg = await _post(async_session, thread, sender_type="agent", sender_id=peer.id,
                      body="need your input", age=dt.timedelta(days=3))

    assert await _poll_ids(client, token) == [str(msg.id)]


@pytest.mark.asyncio
async def test_old_open_question_on_finished_card_does_not_nudge_the_lead(client: AsyncClient, async_session):
    """An `mc ask --to boss` that was never closed keeps the worker's card in
    the lead's scope. Once the card is done and the question is old, nobody is
    waiting for the answer — it must not wake the lead."""
    board = await _board(async_session)
    lead, lead_token = await _agent(async_session, board, lead=True)
    worker, _ = await _agent(async_session, board)
    task = await _task(async_session, board, worker, "done")
    thread = await ensure_task_thread(async_session, task)
    await _post(async_session, thread, sender_type="agent", sender_id=worker.id,
                message_type="question", body="which option?", age=WEEK_AGO,
                question_meta={"awaiting": True, "to": "boss"})

    assert await _poll_ids(client, lead_token) == []


@pytest.mark.asyncio
async def test_open_question_on_active_card_still_reaches_the_lead(client: AsyncClient, async_session):
    board = await _board(async_session)
    lead, lead_token = await _agent(async_session, board, lead=True)
    worker, _ = await _agent(async_session, board)
    task = await _task(async_session, board, worker, "waiting")
    thread = await ensure_task_thread(async_session, task)
    q = await _post(async_session, thread, sender_type="agent", sender_id=worker.id,
                    message_type="question", body="which option?", age=WEEK_AGO,
                    question_meta={"awaiting": True, "to": "boss"})

    assert await _poll_ids(client, lead_token) == [str(q.id)]


# ── Old chat thread ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_old_chat_thread_with_only_own_replies_is_caught_up(client: AsyncClient, async_session):
    """The live chat-thread shape: the agent's own reply sat after the last ack
    forever (own posts are never delivered, and `mc inbox` only acks threads
    it has something to show). It never nudged, but it stayed as phantom
    backlog — now the cursor catches up."""
    board = await _board(async_session)
    agent, token = await _agent(async_session, board)
    chat = await create_chat_thread(async_session, agent, title="old chat")
    asked = await _post(async_session, chat, body="hey, all good?", age=WEEK_AGO * 6)
    await _seen_up_to(async_session, agent, chat, asked.seq)
    own = await _post(async_session, chat, sender_type="agent", sender_id=agent.id,
                      body="all good", age=WEEK_AGO * 6)

    assert await _poll_ids(client, token) == []
    cur = await _cursor(async_session, agent, chat)
    assert cur.last_acked_seq == own.seq


@pytest.mark.asyncio
async def test_old_chat_thread_unread_operator_message_still_nudges(client: AsyncClient, async_session):
    board = await _board(async_session)
    agent, token = await _agent(async_session, board)
    chat = await create_chat_thread(async_session, agent, title="old chat")
    note = await _post(async_session, chat, body="did you see this?", age=WEEK_AGO * 6)

    assert await _poll_ids(client, token) == [str(note.id)]


# ── Ack round trip (what `mc inbox` does) ────────────────────────────────

@pytest.mark.asyncio
async def test_inbox_then_ack_clears_the_nudge(client: AsyncClient, async_session):
    """`mc inbox` = GET /me/inbox, then POST /me/inbox/ack per thread with the
    `threads` max seq. After that round trip /me/poll must be empty, so
    poll.sh drops its remind state."""
    board = await _board(async_session)
    lead, token = await _agent(async_session, board, lead=True)
    dm = await ensure_dm_thread(async_session, lead)
    note = await _post(async_session, dm, body="read me")
    await _post(async_session, dm, sender_type="agent", sender_id=lead.id, body="own")

    body = await _inbox(client, token)
    assert [m["id"] for m in body["messages"]] == [str(note.id)]
    for tid, seq in body["threads"].items():
        resp = await client.post(
            "/api/v1/agent/me/inbox/ack",
            headers={"Authorization": f"Bearer {token}"},
            json={"thread_id": tid, "seq": seq},
        )
        assert resp.status_code == 200

    assert await _poll_ids(client, token) == []


@pytest.mark.asyncio
async def test_stale_window_is_configurable(client: AsyncClient, async_session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "agent_message_stale_after_seconds", 3600)
    board = await _board(async_session)
    lead, token = await _agent(async_session, board, lead=True)
    dm = await ensure_dm_thread(async_session, lead)
    await _post(async_session, dm, sender_type="system", message_type="system",
                body="two hours old", age=dt.timedelta(hours=2))

    assert await _poll_ids(client, token) == []
