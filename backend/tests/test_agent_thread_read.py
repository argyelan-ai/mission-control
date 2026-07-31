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


# ── Paging ───────────────────────────────────────────────────────────────

async def _post_user_messages(async_session, thread, count: int):
    return [
        await post_message(async_session, thread_id=thread.id, sender_type="user",
                           message_type="message", body=f"msg {i + 1}")
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_default_page_is_newest_ascending(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await _post_user_messages(async_session, thread, 5)

    body = (await _thread(client, token, limit=3)).json()
    assert [m["body"] for m in body["messages"]] == ["msg 3", "msg 4", "msg 5"]
    assert body["has_more_before"] is True
    assert body["latest_seq"] == 5


@pytest.mark.asyncio
async def test_since_seq_returns_forward_delta(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    msgs = await _post_user_messages(async_session, thread, 4)

    body = (await _thread(client, token, since_seq=msgs[1].seq)).json()
    assert [m["body"] for m in body["messages"]] == ["msg 3", "msg 4"]


@pytest.mark.asyncio
async def test_before_seq_pages_backwards(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    msgs = await _post_user_messages(async_session, thread, 5)

    body = (await _thread(client, token, before_seq=msgs[2].seq, limit=2)).json()
    assert [m["body"] for m in body["messages"]] == ["msg 1", "msg 2"]
    assert body["has_more_before"] is False


@pytest.mark.asyncio
async def test_since_and_before_are_mutually_exclusive(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    resp = await _thread(client, token, since_seq=1, before_seq=5)
    assert resp.status_code == 400


# ── Degrading gracefully ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_without_thread_reads_as_empty_page(client: AsyncClient, async_session):
    board, agent, token, task, _ = await _board_agent_task(async_session, with_thread=False)

    body = (await _thread(client, token)).json()
    assert body["task_id"] == str(task.id)
    assert body["messages"] == []
    assert body["latest_seq"] == 0

    # GET must not have created the thread.
    await async_session.refresh(task)
    assert task.thread_id is None


@pytest.mark.asyncio
async def test_no_active_task_reads_as_empty_page_not_422(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(
        async_session, status="done", set_current=False,
    )
    resp = await _thread(client, token)
    assert resp.status_code == 200
    assert resp.json()["task_id"] is None


# ── Resolution + authorization ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_falls_back_to_assigned_task_when_pointer_is_null(client: AsyncClient, async_session):
    """current_task_id is NULL by design for non-lead workers under subagent
    dispatch — the assigned-task fallback is what makes the verb usable."""
    board, agent, token, task, thread = await _board_agent_task(
        async_session, set_current=False,
    )
    await _post_user_messages(async_session, thread, 1)

    body = (await _thread(client, token)).json()
    assert body["task_id"] == str(task.id)
    assert [m["body"] for m in body["messages"]] == ["msg 1"]


@pytest.mark.asyncio
async def test_stale_pointer_is_ignored(client: AsyncClient, async_session):
    """A pointer at a task that is no longer active must not win over the
    agent's real current work."""
    board, agent, token, old_task, old_thread = await _board_agent_task(async_session)
    old_task.status = "done"
    async_session.add(old_task)

    fresh = Task(board_id=board.id, assigned_agent_id=agent.id,
                 title="Fresh", status="in_progress")
    async_session.add(fresh)
    await async_session.commit()
    await async_session.refresh(fresh)

    body = (await _thread(client, token)).json()
    assert body["task_id"] == str(fresh.id)


@pytest.mark.asyncio
async def test_cannot_read_another_agents_task(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    _, other_agent, _, other_task, other_thread = await _board_agent_task(async_session)
    await _post_user_messages(async_session, other_thread, 1)

    resp = await _thread(client, token, task_id=str(other_task.id))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unknown_task_id_is_404(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    resp = await _thread(client, token, task_id=str(uuid.uuid4()))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_limit_is_bounded(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    assert (await _thread(client, token, limit=0)).status_code == 422
    assert (await _thread(client, token, limit=201)).status_code == 422


@pytest.mark.asyncio
async def test_reading_is_not_comm_v2_gated(client: AsyncClient, async_session):
    """GET /me/inbox blanks the payload for non-comm_v2 agents because
    *delivery* is the piloted feature. Reading history is not delivery: the
    thread fills up regardless of the flag (operator posts, mc msg), and
    hiding it from an agent trying to recover would recreate exactly the gap
    this endpoint closes. No gate — pinned here so it stays deliberate."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    agent.comm_v2 = False
    async_session.add(agent)
    await async_session.commit()

    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="sichtbar auch ohne pilot")

    body = (await _thread(client, token)).json()
    assert [m["body"] for m in body["messages"]] == ["sichtbar auch ohne pilot"]

    # …while the delivery path stays gated, unchanged.
    inbox = await client.get("/api/v1/agent/me/inbox",
                             headers={"Authorization": f"Bearer {token}"})
    assert inbox.json()["messages"] == []


@pytest.mark.asyncio
async def test_agent_authored_messages_resolve_their_author(client: AsyncClient, async_session):
    """The transcript is only useful if you can tell who said what — the
    agent-sender branch (agent_map lookup) needs its own coverage, since every
    other test here posts as the operator."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="frage vom operator")
    await post_message(async_session, thread_id=thread.id, sender_type="agent",
                       sender_id=agent.id, message_type="message",
                       body="meine eigene antwort")

    body = (await _thread(client, token)).json()
    assert [m["author"]["display"] for m in body["messages"]] == ["Operator", agent.name]
    assert [m["author"]["kind"] for m in body["messages"]] == ["user", "agent"]
    # Delivery state is operator-side bookkeeping — never emitted here.
    assert all("delivery" not in m for m in body["messages"])


@pytest.mark.asyncio
async def test_parked_waiting_task_is_readable(client: AsyncClient, async_session):
    """A task parked in `waiting` is the single strongest case for this verb:
    the agent asked a blocking question, got parked, and comes back wanting to
    read the answer. Parking clears agent.current_task_id while the task stays
    `waiting` and still assigned (task_runner.py:511-513), so the pointer
    branch cannot find it — only the fallback can.

    /me/poll excludes `waiting` from its fallback on purpose: including it
    would re-park the agent. Reading has no claim semantics, so that reason
    does not carry over here."""
    board, agent, token, task, thread = await _board_agent_task(
        async_session, status="waiting", set_current=False,
    )
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="die antwort auf deine frage")

    body = (await _thread(client, token)).json()
    assert body["task_id"] == str(task.id)
    assert [m["body"] for m in body["messages"]] == ["die antwort auf deine frage"]


# ── Character budget ─────────────────────────────────────────────────────
# Everything that lands in an agent's context in this codebase is bounded:
# dispatch 2000/2500/4000, memory 800, lessons 400, the waiting recap 1500,
# mc_logs 4000. A thread read had no bound at all — `limit` caps the number of
# messages, not their size, so 200 long messages were unbounded text. That is
# the same hazard F11 documents for Hermes, whose auto-memory digests whatever
# it is shown.

@pytest.mark.asyncio
async def test_long_thread_is_capped_and_says_so(client: AsyncClient, async_session):
    """Budget trims from the OLD end: a recovering agent needs the newest
    exchange, not the beginning of the task."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    for i in range(12):
        await post_message(async_session, thread_id=thread.id, sender_type="user",
                           message_type="message", body=f"m{i} " + "x" * 600)

    body = (await _thread(client, token)).json()
    total = sum(len(m["body"]) for m in body["messages"])
    assert total <= 4000
    assert body["budget_truncated"] is True
    # Newest kept, oldest dropped.
    assert body["messages"][-1]["body"].startswith("m11")
    assert not any(m["body"].startswith("m0 ") for m in body["messages"])
    # Dropping for budget must still report that older messages exist.
    assert body["has_more_before"] is True


@pytest.mark.asyncio
async def test_single_oversized_message_is_truncated_not_dropped(
    client: AsyncClient, async_session
):
    """One message bigger than the whole budget must still be readable —
    dropping it would leave the agent with an empty page and no explanation."""
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="y" * 9000)

    body = (await _thread(client, token)).json()
    assert len(body["messages"]) == 1
    assert len(body["messages"][0]["body"]) <= 4100  # budget + marker
    assert "gekuerzt" in body["messages"][0]["body"]
    assert body["budget_truncated"] is True


@pytest.mark.asyncio
async def test_short_thread_is_not_flagged_as_truncated(client: AsyncClient, async_session):
    board, agent, token, task, thread = await _board_agent_task(async_session)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="kurz")

    body = (await _thread(client, token)).json()
    assert body["budget_truncated"] is False
    assert body["messages"][0]["body"] == "kurz"


# ── Scope reachability ───────────────────────────────────────────────────
#
# `mc inbox` needs no scope (require_agent only) but `mc thread` needs
# tasks:read, because it also returns the task's title and status. That
# asymmetry is harmless only as long as no role can be *messaged* without
# also being allowed to *re-read* — a role with chat:write but no tasks:read
# would receive nudges and have no way to recover the conversation behind
# them, and would silently lose the verb from its Operating Card.
#
# This is the same failure class PR #195 documented: a stale scope value on
# `inbox` would have taken the verb away from agents without chat:write and
# made them go quiet. Verified 2026-07-31: no role is affected. This test
# exists so that a future role definition cannot introduce one unnoticed.

def test_every_role_that_can_be_messaged_can_also_re_read():
    from app.scopes import AgentRole, Scope, get_default_scopes

    stranded = [
        role.value
        for role in AgentRole
        if Scope.CHAT_WRITE.value in set(get_default_scopes(role))
        and Scope.TASKS_READ.value not in set(get_default_scopes(role))
    ]
    assert not stranded, (
        "role(s) can be sent thread messages but may not call GET /me/thread "
        f"to re-read them: {stranded} — give them tasks:read, or drop the "
        "require_scope on the endpoint"
    )
