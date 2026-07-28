"""Tests for the agent-side thread READ API.

GET /api/v1/agent/me/thread lets an agent re-read its own task thread to
rebuild context after a restart. It is a pure look-up: unlike `mc inbox` it
consumes nothing, so an agent that reads its thread still receives every
unacked message afterwards.

Fixtures mirror test_inbox_pull.py (agent-token auth) and the pagination cases
mirror test_thread_read_api.py (the operator-side equivalent).
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.thread import AgentThreadCursor
from app.services.messaging import ensure_task_thread, post_message


async def _board_agent_task(
    async_session: AsyncSession,
    *,
    status: str = "in_progress",
    assign: bool = True,
    with_thread: bool = True,
    set_current: bool = True,
):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Rex-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        board_id=board.id,
        comm_v2=True,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id if assign else None,
        title="Thread probe",
        status=status,
        dispatched_at=now,
        ack_at=now,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    thread = None
    if with_thread:
        thread = await ensure_task_thread(async_session, task)

    if set_current:
        agent.current_task_id = task.id
        async_session.add(agent)
        await async_session.commit()
        await async_session.refresh(agent)

    return board, agent, raw_token, task, thread


async def _thread(client: AsyncClient, token: str, **params):
    return await client.get(
        "/api/v1/agent/me/thread",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


async def _cursor_count(async_session: AsyncSession, agent, thread) -> int:
    return (
        await async_session.exec(
            select(func.count()).select_from(AgentThreadCursor).where(
                AgentThreadCursor.agent_id == agent.id,
                AgentThreadCursor.thread_id == thread.id,
            )
        )
    ).one()


# ── The rule this endpoint exists to obey ────────────────────────────────

@pytest.mark.asyncio
async def test_read_does_not_touch_an_existing_cursor(client: AsyncClient, async_session):
    """Reading the thread must not consume unread mail: both cursor columns
    stay put and `mc inbox` still delivers the same messages afterwards."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="unread one")
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="unread two")

    cursor = AgentThreadCursor(agent_id=agent.id, thread_id=thread.id,
                              last_delivered_seq=0, last_acked_seq=0)
    async_session.add(cursor)
    await async_session.commit()

    resp = await _thread(client, token)
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2

    await async_session.refresh(cursor)
    assert cursor.last_acked_seq == 0
    assert cursor.last_delivered_seq == 0

    # The messages are still deliverable — nothing was consumed.
    inbox = await client.get("/api/v1/agent/me/inbox",
                             headers={"Authorization": f"Bearer {token}"})
    assert [m["body"] for m in inbox.json()["messages"]] == ["unread one", "unread two"]


@pytest.mark.asyncio
async def test_read_creates_no_cursor(client: AsyncClient, async_session):
    """No cursor row exists yet — reading must not create one. This is the
    trap in _resolve_agent_threads_with_cursors that this endpoint avoids."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="hello")
    assert await _cursor_count(async_session, agent, thread) == 0

    resp = await _thread(client, token)
    assert resp.status_code == 200
    assert await _cursor_count(async_session, agent, thread) == 0


@pytest.mark.asyncio
async def test_read_does_not_fast_forward_a_finished_task(client: AsyncClient, async_session):
    """A done task's thread is exactly what a returning agent wants to re-read,
    and reading it must not fast-forward anything (Befund C, PR #150)."""
    board, agent, token, task, thread = await _board_agent_task(async_session, status="done")
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="history")

    resp = await _thread(client, token, task_id=str(task.id))
    assert resp.status_code == 200
    assert [m["body"] for m in resp.json()["messages"]] == ["history"]
    assert await _cursor_count(async_session, agent, thread) == 0
