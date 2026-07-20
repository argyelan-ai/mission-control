"""Tests for app.services.messaging — post_message, seq allocation, task-thread
autocreate, awaiting semantics (Task 3, Interaction Model 2.0).

Consumes Task 1 (comm_constants.MESSAGE_TYPES) and Task 2 (Thread/Message/
AgentThreadCursor models). See task-3-brief.md scenarios (a)-(d).
"""
from __future__ import annotations

import pytest

from app.models.board import Board
from app.models.task import Task
from app.models.thread import Thread, Message
from app.services.messaging import (
    ensure_task_thread,
    post_message,
    answer_clears_awaiting,
    open_questions,
)


@pytest.fixture
async def board(session) -> Board:
    b = Board(name="test", slug="t", group_id=None)
    session.add(b)
    await session.commit()
    await session.refresh(b)
    return b


@pytest.fixture
async def task(session, board: Board) -> Task:
    t = Task(title="messaging-test", status="inbox", board_id=board.id)
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@pytest.fixture
async def thread(session) -> Thread:
    t = Thread(kind="side")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


# (a) post_message vergibt 1,2,3 auf demselben Thread
@pytest.mark.asyncio
async def test_post_message_assigns_incrementing_seq(session, thread: Thread):
    m1 = await post_message(
        session, thread_id=thread.id, sender_type="system", body="one",
    )
    m2 = await post_message(
        session, thread_id=thread.id, sender_type="system", body="two",
    )
    m3 = await post_message(
        session, thread_id=thread.id, sender_type="system", body="three",
    )
    assert (m1.seq, m2.seq, m3.seq) == (1, 2, 3)
    assert m1.thread_id == thread.id == m2.thread_id == m3.thread_id


# (b) message_type="question" ohne question_meta -> ValueError
@pytest.mark.asyncio
async def test_question_without_question_meta_raises(session, thread: Thread):
    with pytest.raises(ValueError):
        await post_message(
            session,
            thread_id=thread.id,
            sender_type="agent",
            message_type="question",
            body="Was denkst du?",
        )


@pytest.mark.asyncio
async def test_invalid_message_type_raises(session, thread: Thread):
    with pytest.raises(ValueError):
        await post_message(
            session,
            thread_id=thread.id,
            sender_type="agent",
            message_type="not-a-real-type",
            body="x",
        )


# (c) Antwort mit reply_to auf offene question -> awaiting wird False
@pytest.mark.asyncio
async def test_answer_clears_awaiting(session, thread: Thread):
    question = await post_message(
        session,
        thread_id=thread.id,
        sender_type="agent",
        message_type="question",
        body="Soll ich X tun?",
        question_meta={"awaiting": True, "to": "mark", "priority": "medium"},
    )
    assert question.question_meta["awaiting"] is True

    answer = await post_message(
        session,
        thread_id=thread.id,
        sender_type="user",
        sender_id=None,
        body="Ja, mach das.",
        reply_to=question.id,
    )
    await answer_clears_awaiting(session, answer)
    await session.refresh(question)
    assert question.question_meta["awaiting"] is False


@pytest.mark.asyncio
async def test_answer_clears_awaiting_noop_when_not_reply_to_question(
    session, thread: Thread
):
    plain = await post_message(
        session, thread_id=thread.id, sender_type="system", body="fyi",
    )
    reply = await post_message(
        session,
        thread_id=thread.id,
        sender_type="agent",
        body="ack",
        reply_to=plain.id,
    )
    # Should not raise; plain message has no question_meta at all.
    await answer_clears_awaiting(session, reply)
    await session.refresh(plain)
    assert plain.question_meta is None


# (d) ensure_task_thread ist idempotent
@pytest.mark.asyncio
async def test_ensure_task_thread_idempotent(session, task: Task):
    thread1 = await ensure_task_thread(session, task)
    assert thread1.kind == "task"
    assert thread1.task_id == task.id
    assert task.thread_id == thread1.id

    thread2 = await ensure_task_thread(session, task)
    assert thread2.id == thread1.id
    assert task.thread_id == thread1.id


@pytest.mark.asyncio
async def test_open_questions_filters_by_thread_and_target(session, thread: Thread):
    t2 = Thread(kind="side")
    session.add(t2)
    await session.commit()
    await session.refresh(t2)

    q1 = await post_message(
        session,
        thread_id=thread.id,
        sender_type="agent",
        message_type="question",
        body="q1 to mark",
        question_meta={"awaiting": True, "to": "mark", "priority": "medium"},
    )
    await post_message(
        session,
        thread_id=thread.id,
        sender_type="agent",
        message_type="question",
        body="q2 to boss, already answered",
        question_meta={"awaiting": False, "to": "boss", "priority": "low"},
    )
    q3 = await post_message(
        session,
        thread_id=t2.id,
        sender_type="agent",
        message_type="question",
        body="q3 in other thread, to mark",
        question_meta={"awaiting": True, "to": "mark", "priority": "high"},
    )

    by_thread = await open_questions(session, thread_id=thread.id)
    assert [m.id for m in by_thread] == [q1.id]

    by_target = await open_questions(session, to="mark")
    ids = {m.id for m in by_target}
    assert ids == {q1.id, q3.id}
