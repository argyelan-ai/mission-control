"""`mc ask --to boss` must reach the board lead (incident 10.09.2026).

A worker asked its lead on the thread of ITS OWN subtask. Thread delivery
(`thread_scope.message_threads_for_agent`) only covered the threads of the
agent's own active tasks, DMs and groups — the lead, holding only the parent
task, never saw the question; the worker waited a round and decided alone.

Now a board lead also takes part in every task thread of its board that
carries a still-open question addressed to "boss": the question shows up in
the lead's poll `new_messages` / `mc inbox`, and the lead may reply there.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.services.messaging import ensure_task_thread, post_message
from app.services.thread_scope import message_threads_for_agent, thread_agent_may_write_to
from tests.conftest import test_engine


async def _setup(s: AsyncSession):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    s.add(board)
    await s.commit()
    lead_raw, lead_hash = generate_agent_token()
    lead = Agent(name="the lead", agent_runtime="host", agent_token_hash=lead_hash,
                 board_id=board.id, is_board_lead=True, scopes=["heartbeat", "tasks:read", "chat:write"], comm_v2=True)
    w_raw, w_hash = generate_agent_token()
    worker = Agent(name="the worker", agent_runtime="cli-bridge", agent_token_hash=w_hash,
                   board_id=board.id, scopes=["heartbeat", "tasks:read"], comm_v2=True)
    s.add(lead); s.add(worker)
    await s.commit()
    parent = Task(board_id=board.id, title="parent", status="in_progress", assigned_agent_id=lead.id)
    s.add(parent)
    await s.commit()
    sub = Task(board_id=board.id, title="sub", status="in_progress",
               assigned_agent_id=worker.id, parent_task_id=parent.id)
    s.add(sub)
    await s.commit()
    await s.refresh(sub)
    thread = await ensure_task_thread(s, sub)
    await s.commit()
    return lead, lead_raw, worker, sub, thread


async def _ask(s: AsyncSession, thread_id, worker, *, to="boss", awaiting=True):
    m = await post_message(
        s, thread_id=thread_id, sender_type="agent", sender_id=worker.id,
        message_type="question", body="Widerspruch in der Karte?",
        question_meta={"awaiting": awaiting, "blocking": False, "to": to},
    )
    await s.commit()
    return m


@pytest.mark.asyncio
async def test_open_question_to_boss_reaches_lead_poll(client: AsyncClient, async_session):
    lead, lead_raw, worker, sub, thread = await _setup(async_session)
    q = await _ask(async_session, thread.id, worker)
    body = (await client.get("/api/v1/agent/me/poll",
                             headers={"Authorization": f"Bearer {lead_raw}"})).json()
    ids = [m["id"] for m in (body.get("new_messages") or [])]
    assert str(q.id) in ids, "the lead must be delivered the worker's question"


@pytest.mark.asyncio
async def test_lead_may_reply_on_the_asking_thread(async_session):
    lead, _, worker, sub, thread = await _setup(async_session)
    await _ask(async_session, thread.id, worker)
    assert await thread_agent_may_write_to(async_session, lead, thread.id) is not None


@pytest.mark.asyncio
async def test_question_without_boss_target_is_not_routed(async_session):
    """Sabotage: a plain message / a question to 'mark' stays where it is."""
    lead, _, worker, sub, thread = await _setup(async_session)
    await _ask(async_session, thread.id, worker, to="mark")
    pairs = await message_threads_for_agent(lead, async_session)
    assert thread.id not in {t.id for t, _ in pairs}


@pytest.mark.asyncio
async def test_lead_reply_via_endpoint_answers_and_drops_the_thread(client: AsyncClient, async_session):
    """Review #496 B2: the ONLY way the lead answers in the field is
    `mc msg --thread` = POST /agent/threads/{id}/messages without reply_to.
    That must count as the answer to the oldest open question — afterwards
    the thread leaves the lead's scope again."""
    lead, lead_raw, worker, sub, thread = await _setup(async_session)
    q = await _ask(async_session, thread.id, worker)
    resp = await client.post(
        f"/api/v1/agent/threads/{thread.id}/messages",
        json={"body": "Nimm 1200/250/250."},
        headers={"Authorization": f"Bearer {lead_raw}"},
    )
    assert resp.status_code == 201, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.thread import Message
        fresh = (await s.exec(select(Message).where(Message.id == q.id))).one()
        assert fresh.question_meta["awaiting"] is False, "lead's reply must close the question"
        lead_row = await s.get(Agent, lead.id)
        pairs = await message_threads_for_agent(lead_row, s)
        assert thread.id not in {t.id for t, _ in pairs}


@pytest.mark.asyncio
async def test_open_question_on_finished_task_is_still_delivered(client: AsyncClient, async_session):
    """Review #496 B1 = the incident's end state: worker asks, gets nothing,
    finishes alone (task done). The lead's FIRST poll must still deliver the
    question — no fast-forward past it."""
    lead, lead_raw, worker, sub, thread = await _setup(async_session)
    q = await _ask(async_session, thread.id, worker)
    sub.status = "done"
    async_session.add(sub)
    await async_session.commit()
    body = (await client.get("/api/v1/agent/me/poll",
                             headers={"Authorization": f"Bearer {lead_raw}"})).json()
    assert str(q.id) in [m["id"] for m in (body.get("new_messages") or [])]


@pytest.mark.asyncio
async def test_first_sight_delivers_question_not_card_history(client: AsyncClient, async_session):
    """Review #496 W1: six progress messages before the question must NOT be
    replayed into the lead's context — delivery starts at the question."""
    lead, lead_raw, worker, sub, thread = await _setup(async_session)
    for i in range(6):
        await post_message(async_session, thread_id=thread.id, sender_type="agent",
                           sender_id=worker.id, message_type="message", body=f"Zwischenstand {i}")
    await async_session.commit()
    q = await _ask(async_session, thread.id, worker)
    body = (await client.get("/api/v1/agent/me/poll",
                             headers={"Authorization": f"Bearer {lead_raw}"})).json()
    delivered = [m["id"] for m in (body.get("new_messages") or [])]
    assert delivered[-1] == str(q.id) and len(delivered) <= 4, f"expected the question + <=3 context lines, got {len(delivered)}"


@pytest.mark.asyncio
async def test_other_boards_lead_does_not_see_it(async_session):
    lead, _, worker, sub, thread = await _setup(async_session)
    await _ask(async_session, thread.id, worker)
    other_board = Board(name="O", slug=f"o-{uuid.uuid4().hex[:6]}")
    async_session.add(other_board)
    await async_session.commit()
    _, h = generate_agent_token()
    other_lead = Agent(name="other lead", agent_runtime="host", agent_token_hash=h,
                       board_id=other_board.id, is_board_lead=True, scopes=["heartbeat"], comm_v2=True)
    async_session.add(other_lead)
    await async_session.commit()
    pairs = await message_threads_for_agent(other_lead, async_session)
    assert thread.id not in {t.id for t, _ in pairs}


# ── Review #496 round 3: B3 (no self-lockout), B4 (blocking ask resumes), context lines ──

@pytest.mark.asyncio
async def test_lead_status_post_does_not_close_question_and_real_answer_still_lands(client: AsyncClient, async_session):
    """B3 dead-end: a `status` line must not count as the answer, and the lead
    must keep the write right on the thread until the real answer is posted."""
    lead, lead_raw, worker, sub, thread = await _setup(async_session)
    q = await _ask(async_session, thread.id, worker)
    h = {"Authorization": f"Bearer {lead_raw}"}
    r1 = await client.post(f"/api/v1/agent/threads/{thread.id}/messages",
                           json={"body": "moment, schaue ich mir an", "message_type": "status"}, headers=h)
    assert r1.status_code == 201, r1.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.thread import Message
        still_open = (await s.exec(select(Message).where(Message.id == q.id))).one()
        assert still_open.question_meta["awaiting"] is True, "a status line is not an answer"
    r2 = await client.post(f"/api/v1/agent/threads/{thread.id}/messages",
                           json={"body": "Nimm 1200/250/250."}, headers=h)
    assert r2.status_code == 201, "the real answer must still be postable (no self-lockout)"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.thread import Message
        closed = (await s.exec(select(Message).where(Message.id == q.id))).one()
        assert closed.question_meta["awaiting"] is False
    # and the lead keeps the write right on the board's task thread afterwards
    r3 = await client.post(f"/api/v1/agent/threads/{thread.id}/messages",
                           json={"body": "Nachtrag", "message_type": "status"}, headers=h)
    assert r3.status_code == 201


@pytest.mark.asyncio
async def test_lead_answer_to_blocking_question_resumes_waiting_worker(client: AsyncClient, async_session):
    """B4: the worker parked itself with `mc ask --blocking` (task waiting);
    the lead's answer must release it back to in_progress."""
    lead, lead_raw, worker, sub, thread = await _setup(async_session)
    from unittest.mock import AsyncMock, patch
    await post_message(async_session, thread_id=thread.id, sender_type="agent", sender_id=worker.id,
                       message_type="question", body="Darf ich X?",
                       question_meta={"awaiting": True, "blocking": True, "to": "boss"})
    sub.status = "waiting"
    async_session.add(sub)
    await async_session.commit()
    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock), \
         patch("app.utils.create_tracked_task") as tracked:
        tracked.side_effect = lambda coro, name=None: coro.close()
        r = await client.post(f"/api/v1/agent/threads/{thread.id}/messages",
                              json={"body": "Ja, mach X."},
                              headers={"Authorization": f"Bearer {lead_raw}"})
    assert r.status_code == 201, r.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, sub.id)
        assert fresh.status == "in_progress", "answered blocking ask must release the worker"


@pytest.mark.asyncio
async def test_first_sight_includes_three_context_lines_before_question(client: AsyncClient, async_session):
    lead, lead_raw, worker, sub, thread = await _setup(async_session)
    for i in range(6):
        await post_message(async_session, thread_id=thread.id, sender_type="agent",
                           sender_id=worker.id, message_type="message", body=f"Zwischenstand {i}")
    await async_session.commit()
    q = await _ask(async_session, thread.id, worker)
    body = (await client.get("/api/v1/agent/me/poll",
                             headers={"Authorization": f"Bearer {lead_raw}"})).json()
    delivered = [m["body"] for m in (body.get("new_messages") or [])]
    assert delivered[-1].startswith("Widerspruch") and delivered[:-1] == ["Zwischenstand 3", "Zwischenstand 4", "Zwischenstand 5"], delivered
