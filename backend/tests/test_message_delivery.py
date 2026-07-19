"""Tests for the two-stage cursor message delivery in /agent/me/poll (Task 4).

Interaction Model 2.0 (§9.1) at-least-once delivery: a poll delivers new
Messages on the agent's active task thread and advances
`last_delivered_seq`; only the *next* poll carrying `acked_seq` advances
`last_acked_seq`. Between delivery and ack the messages sit in the redelivery
window `(last_acked_seq, last_delivered_seq]`, so a lost poll re-delivers.

The `comm_v2` agent flag lands in Task 11 — until then the poll gates the new
`new_messages` field behind `getattr(agent, "comm_v2", False)`. These tests
flip it on via a class-level monkeypatch so `getattr` resolves True for the
endpoint's freshly-loaded Agent instance.
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
from app.services.messaging import ensure_task_thread, post_message


@pytest.fixture(autouse=True)
def _enable_comm_v2(monkeypatch):
    """Task 11 adds Agent.comm_v2; until then expose it as a class attr so the
    poll endpoint's `getattr(agent, "comm_v2", False)` gate resolves True."""
    monkeypatch.setattr(Agent, "comm_v2", True, raising=False)


async def _board_agent_task(async_session: AsyncSession):
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
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Thread probe",
        status="in_progress",
        dispatched_at=now,
        ack_at=now,  # already acked → poll returns state=working, no prompt build
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    thread = await ensure_task_thread(async_session, task)
    return board, agent, raw_token, task, thread


async def _poll(client: AsyncClient, token: str, acked: dict | None = None):
    params = {}
    if acked is not None:
        params["acked_seq"] = json.dumps(acked)
    return await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


@pytest.mark.asyncio
async def test_happy_path_deliver_then_ack(client: AsyncClient, async_session):
    """(a) Deliver on poll 1 (last_delivered_seq==seq); poll 2 with acked_seq
    sets last_acked_seq==seq and does not re-deliver."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="hello agent",
    )

    resp = await _poll(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "working"
    ids = [m["id"] for m in body["new_messages"]]
    assert ids == [str(msg.id)]
    assert body["new_messages"][0]["seq"] == msg.seq
    assert body["new_messages"][0]["body"] == "hello agent"

    cur = (
        await async_session.exec(
            select(AgentThreadCursor).where(
                AgentThreadCursor.agent_id == agent.id,
                AgentThreadCursor.thread_id == thread.id,
            )
        )
    ).one()
    await async_session.refresh(cur)
    assert cur.last_delivered_seq == msg.seq
    assert cur.last_acked_seq == 0

    resp2 = await _poll(client, token, acked={str(thread.id): msg.seq})
    body2 = resp2.json()
    assert body2["new_messages"] == []

    await async_session.refresh(cur)
    assert cur.last_acked_seq == msg.seq


@pytest.mark.asyncio
async def test_lost_poll_redelivers(client: AsyncClient, async_session):
    """(b) Poll 1 delivers; no ack follows; poll 2 without acked_seq re-delivers
    the same message (at-least-once)."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="did you get this?",
    )

    first = await _poll(client, token)
    assert [m["id"] for m in first.json()["new_messages"]] == [str(msg.id)]

    # The delivery poll was "lost" — the agent never sends acked_seq.
    second = await _poll(client, token)
    assert [m["id"] for m in second.json()["new_messages"]] == [str(msg.id)]


@pytest.mark.asyncio
async def test_ordering_by_seq_same_created_at(client: AsyncClient, async_session):
    """(c) Three messages with identical created_at, seq 1..3 → delivered in
    seq order regardless of insertion/timestamp ties."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    ts = dt.datetime.now(tz=dt.timezone.utc)
    made = []
    for i in range(1, 4):
        m = Message(
            thread_id=thread.id,
            seq=i,
            sender_type="user",
            message_type="message",
            body=f"m{i}",
            created_at=ts,
        )
        async_session.add(m)
        made.append(m)
    await async_session.commit()

    body = (await _poll(client, token)).json()
    seqs = [m["seq"] for m in body["new_messages"]]
    assert seqs == [1, 2, 3]


@pytest.mark.asyncio
async def test_own_messages_not_delivered_but_advance_cursor(client: AsyncClient, async_session):
    """Agent's own posts advance the cursor but are never delivered back to it
    (otherwise a redelivery loop of its own messages)."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    own = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="agent",
        sender_id=agent.id,
        message_type="message",
        body="my own note",
    )
    user_msg = await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="reply from user",
    )

    body = (await _poll(client, token)).json()
    ids = [m["id"] for m in body["new_messages"]]
    assert str(own.id) not in ids
    assert ids == [str(user_msg.id)]

    cur = (
        await async_session.exec(
            select(AgentThreadCursor).where(
                AgentThreadCursor.agent_id == agent.id,
                AgentThreadCursor.thread_id == thread.id,
            )
        )
    ).one()
    await async_session.refresh(cur)
    assert cur.last_delivered_seq == user_msg.seq


@pytest.mark.asyncio
async def test_new_messages_absent_without_comm_v2(client: AsyncClient, async_session, monkeypatch):
    """Non-pilot agents (no comm_v2) never see the new_messages field."""
    monkeypatch.setattr(Agent, "comm_v2", False, raising=False)
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await post_message(
        async_session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body="hidden",
    )
    body = (await _poll(client, token)).json()
    assert "new_messages" not in body
    assert "new_comments" in body
