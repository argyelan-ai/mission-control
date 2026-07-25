"""Tests for the user-side thread READ API (comm_v2, UI THREAD panel).

GET /api/v1/tasks/{task_id}/thread — seq-paged read (newest-page default,
since_seq forward delta, before_seq backward pagination) with recipient
derivation, delivery derivation from the recipient agent's AgentThreadCursor,
and the caller's own read cursor (my_read_seq).
POST /api/v1/tasks/{task_id}/thread/read — advances that cursor (idempotent,
never backwards).

Fixtures mirror test_inbox_pull.py: board + agent + task rows directly via
async_session, messages via the messaging service.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.thread import AgentThreadCursor
from app.services.messaging import ensure_task_thread, post_message


async def _board_agent_task(
    async_session: AsyncSession,
    *,
    assign: bool = True,
    agent_status: str = "offline",
):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    agent = Agent(
        name=f"Boss {uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        board_id=board.id,
        is_board_lead=True,
        status=agent_status,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    task = Task(
        board_id=board.id,
        title="Thread read probe",
        status="in_progress",
        assigned_agent_id=agent.id if assign else None,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    return board, agent, task


async def _post_user_messages(async_session: AsyncSession, thread, count: int):
    msgs = []
    for i in range(count):
        msgs.append(await post_message(
            async_session,
            thread_id=thread.id,
            sender_type="user",
            message_type="message",
            body=f"msg {i + 1}",
        ))
    return msgs


def _thread_url(task) -> str:
    return f"/api/v1/tasks/{task.id}/thread"


# ── Empty thread ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_thread_returns_empty_page(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    await ensure_task_thread(async_session, task)

    resp = await auth_client.get(_thread_url(task))
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == str(task.id)
    assert body["messages"] == []
    assert body["has_more_before"] is False
    assert body["latest_seq"] == 0
    assert body["my_read_seq"] == 0
    assert body["recipient"] == {
        "kind": "agent",
        "id": agent.slug,
        "display": agent.name,
        "listening": False,  # agent_status defaults to offline
        "reason": "assignee",
    }


@pytest.mark.asyncio
async def test_threadless_task_reads_as_empty_page(auth_client: AsyncClient, async_session):
    """A task that was never messaged (no lazily created thread) reads as an
    empty page instead of 404 — GET never creates the thread."""
    board, agent, task = await _board_agent_task(async_session)
    assert task.thread_id is None

    resp = await auth_client.get(_thread_url(task))
    assert resp.status_code == 200
    body = resp.json()
    assert body["messages"] == []
    assert body["has_more_before"] is False
    assert body["latest_seq"] == 0
    assert body["my_read_seq"] == 0
    assert body["recipient"]["reason"] == "assignee"


@pytest.mark.asyncio
async def test_unknown_task_404(auth_client: AsyncClient):
    resp = await auth_client.get(f"/api/v1/tasks/{uuid.uuid4()}/thread")
    assert resp.status_code == 404


# ── Paging ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_page_is_newest_limit_ascending(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)
    await _post_user_messages(async_session, thread, 60)

    resp = await auth_client.get(_thread_url(task))
    assert resp.status_code == 200
    body = resp.json()
    seqs = [m["seq"] for m in body["messages"]]
    assert seqs == list(range(11, 61))  # newest 50, ascending
    assert body["has_more_before"] is True
    assert body["latest_seq"] == 60

    resp = await auth_client.get(_thread_url(task), params={"limit": 5})
    body = resp.json()
    assert [m["seq"] for m in body["messages"]] == [56, 57, 58, 59, 60]
    assert body["has_more_before"] is True


@pytest.mark.asyncio
async def test_exactly_limit_messages_has_no_more_before(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)
    await _post_user_messages(async_session, thread, 50)

    resp = await auth_client.get(_thread_url(task))
    body = resp.json()
    assert [m["seq"] for m in body["messages"]] == list(range(1, 51))
    assert body["has_more_before"] is False


@pytest.mark.asyncio
async def test_since_seq_forward_delta(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)
    await _post_user_messages(async_session, thread, 10)

    resp = await auth_client.get(_thread_url(task), params={"since_seq": 7})
    body = resp.json()
    assert [m["seq"] for m in body["messages"]] == [8, 9, 10]
    assert body["has_more_before"] is True
    assert body["latest_seq"] == 10

    # Up-to-date client: empty delta, latest_seq still reports the head.
    resp = await auth_client.get(_thread_url(task), params={"since_seq": 10})
    body = resp.json()
    assert body["messages"] == []
    assert body["has_more_before"] is False
    assert body["latest_seq"] == 10


@pytest.mark.asyncio
async def test_before_seq_backward_pagination(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)
    await _post_user_messages(async_session, thread, 20)

    resp = await auth_client.get(_thread_url(task), params={"before_seq": 21, "limit": 10})
    body = resp.json()
    assert [m["seq"] for m in body["messages"]] == list(range(11, 21))
    assert body["has_more_before"] is True

    # Walk one page older with before_seq = messages[0].seq — reaches the
    # oldest page, so the "Load older" flag clears.
    resp = await auth_client.get(_thread_url(task), params={"before_seq": 11, "limit": 10})
    body = resp.json()
    assert [m["seq"] for m in body["messages"]] == list(range(1, 11))
    assert body["has_more_before"] is False


@pytest.mark.asyncio
async def test_since_and_before_seq_conflict_400(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    await ensure_task_thread(async_session, task)

    resp = await auth_client.get(_thread_url(task), params={"since_seq": 1, "before_seq": 5})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_limit_is_capped_at_200(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    await ensure_task_thread(async_session, task)

    resp = await auth_client.get(_thread_url(task), params={"limit": 201})
    assert resp.status_code == 422
    resp = await auth_client.get(_thread_url(task), params={"limit": 0})
    assert resp.status_code == 422


# ── Message shape, author, delivery ───────────────────────────────────────

@pytest.mark.asyncio
async def test_message_shape_and_author_blocks(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)
    await post_message(async_session, thread_id=thread.id, sender_type="user",
                       message_type="message", body="from the operator")
    await post_message(async_session, thread_id=thread.id, sender_type="agent",
                       sender_id=agent.id, message_type="message", body="from the agent")
    await post_message(async_session, thread_id=thread.id, sender_type="system",
                       message_type="system", body="system line")

    resp = await auth_client.get(_thread_url(task))
    body = resp.json()
    user_msg, agent_msg, system_msg = body["messages"]

    assert user_msg["direction"] == "user_to_agent"
    assert user_msg["author"] == {"kind": "user", "id": None, "display": "Operator"}
    assert user_msg["body"] == "from the operator"
    assert user_msg["body_format"] == "text"
    assert user_msg["created_at"].endswith("Z")
    assert user_msg["delivery"] == "queued"  # no agent cursor yet

    assert agent_msg["direction"] == "agent_to_user"
    assert agent_msg["author"] == {"kind": "agent", "id": agent.slug, "display": agent.name}
    assert "delivery" not in agent_msg

    assert system_msg["direction"] == "system"
    assert system_msg["author"] == {"kind": "system", "id": None, "display": "System"}
    assert "delivery" not in system_msg


@pytest.mark.asyncio
async def test_delivery_derived_from_recipient_agent_cursor(auth_client: AsyncClient, async_session):
    """user_to_agent delivery: seq <= last_acked_seq → read,
    seq <= last_delivered_seq → delivered, else queued."""
    board, agent, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)
    await _post_user_messages(async_session, thread, 4)
    async_session.add(AgentThreadCursor(
        agent_id=agent.id,
        thread_id=thread.id,
        last_delivered_seq=3,
        last_acked_seq=1,
    ))
    await async_session.commit()

    resp = await auth_client.get(_thread_url(task))
    body = resp.json()
    deliveries = {m["seq"]: m["delivery"] for m in body["messages"]}
    assert deliveries == {1: "read", 2: "delivered", 3: "delivered", 4: "queued"}


# ── Recipient derivation ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recipient_listening_when_agent_active(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session, agent_status="working")
    await ensure_task_thread(async_session, task)

    resp = await auth_client.get(_thread_url(task))
    recipient = resp.json()["recipient"]
    assert recipient["listening"] is True
    assert recipient["reason"] == "assignee"


@pytest.mark.asyncio
async def test_recipient_falls_back_to_board_lead(auth_client: AsyncClient, async_session):
    """No assignee → the (non-archived) board lead is the thread recipient,
    mirroring the callback_agent_id fallback for unrouted traffic."""
    board, agent, task = await _board_agent_task(async_session, assign=False, agent_status="idle")
    await ensure_task_thread(async_session, task)

    resp = await auth_client.get(_thread_url(task))
    recipient = resp.json()["recipient"]
    assert recipient["kind"] == "agent"
    assert recipient["id"] == agent.slug
    assert recipient["reason"] == "board_lead"
    assert recipient["listening"] is True


# ── Read marker ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_marker_roundtrip_and_never_backwards(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    thread = await ensure_task_thread(async_session, task)
    await _post_user_messages(async_session, thread, 5)

    resp = await auth_client.post(_thread_url(task) + "/read", json={"last_read_seq": 3})
    assert resp.status_code == 204
    assert resp.content == b""

    body = (await auth_client.get(_thread_url(task))).json()
    assert body["my_read_seq"] == 3

    # Never backwards: a stale marker leaves the cursor untouched.
    resp = await auth_client.post(_thread_url(task) + "/read", json={"last_read_seq": 1})
    assert resp.status_code == 204
    body = (await auth_client.get(_thread_url(task))).json()
    assert body["my_read_seq"] == 3

    # Forward again.
    resp = await auth_client.post(_thread_url(task) + "/read", json={"last_read_seq": 5})
    assert resp.status_code == 204
    body = (await auth_client.get(_thread_url(task))).json()
    assert body["my_read_seq"] == 5


@pytest.mark.asyncio
async def test_read_marker_creates_thread_lazily(auth_client: AsyncClient, async_session):
    """The marker may land before the first message — POST creates the task
    thread (like the message POST does) and stores the cursor against it."""
    board, agent, task = await _board_agent_task(async_session)
    assert task.thread_id is None

    resp = await auth_client.post(_thread_url(task) + "/read", json={"last_read_seq": 0})
    assert resp.status_code == 204

    body = (await auth_client.get(_thread_url(task))).json()
    assert body["my_read_seq"] == 0


@pytest.mark.asyncio
async def test_read_marker_rejects_negative_seq(auth_client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)
    resp = await auth_client.post(_thread_url(task) + "/read", json={"last_read_seq": -1})
    assert resp.status_code == 422


# ── Auth ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_required(client: AsyncClient, async_session):
    board, agent, task = await _board_agent_task(async_session)

    resp = await client.get(_thread_url(task))
    assert resp.status_code == 401
    resp = await client.post(_thread_url(task) + "/read", json={"last_read_seq": 1})
    assert resp.status_code == 401
