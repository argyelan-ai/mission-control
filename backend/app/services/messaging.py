"""Messaging service — Interaction Model 2.0 (§9.1).

Owns the append-only write path for Thread/Message (Task 2 models):
- ensure_task_thread: lazily creates the one Thread(kind="task") per Task.
- post_message: atomic seq allocation + message_type/question_meta validation.
- answer_clears_awaiting: communication-state mutation allowed by §3.3 —
  answering a question clears its awaiting flag.
- open_questions: query helper for outstanding questions.

Consumes Task 1 (app.comm_constants) and Task 2 (app.models.thread).
"""
from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.comm_constants import MESSAGE_TYPES
from app.models.task import Task, TaskComment
from app.models.thread import Thread, Message
from app.utils import ensure_aware

FINISH_NUDGE_BODY = "Du hast Fertigstellung signalisiert — bitte `mc finish` ausführen"


async def _next_seq(session: AsyncSession, thread_id: uuid.UUID) -> int:
    """Allocate the next seq for a thread.

    Postgres: locks the Thread row (SELECT ... FOR UPDATE) so concurrent
    posters can't race to the same seq. SQLite (tests): same codepath minus
    the row lock — sqlite has no concurrent writers in-process anyway.
    """
    stmt = select(Thread).where(Thread.id == thread_id)
    if session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    result = await session.exec(stmt)
    thread = result.one()

    max_seq_result = await session.exec(
        select(func.coalesce(func.max(Message.seq), 0)).where(Message.thread_id == thread.id)
    )
    return max_seq_result.one() + 1


async def ensure_task_thread(session: AsyncSession, task: Task) -> Thread:
    """Return task's Thread(kind="task"), creating it on first use. Idempotent."""
    if task.thread_id is not None:
        result = await session.exec(select(Thread).where(Thread.id == task.thread_id))
        existing = result.one_or_none()
        if existing is not None:
            return existing

    thread = Thread(kind="task", task_id=task.id)
    session.add(thread)
    await session.commit()
    await session.refresh(thread)

    task.thread_id = thread.id
    session.add(task)
    await session.commit()
    await session.refresh(task)

    return thread


async def post_message(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID,
    sender_type: str,
    sender_id: uuid.UUID | None = None,
    message_type: str = "message",
    body: str,
    reply_to: uuid.UUID | None = None,
    mentions: list[str] | None = None,
    question_meta: dict | None = None,
) -> Message:
    """Post a message onto a thread, allocating its seq atomically."""
    if message_type not in MESSAGE_TYPES:
        raise ValueError(
            f"invalid message_type {message_type!r}; must be one of {MESSAGE_TYPES}"
        )
    if message_type == "question" and not question_meta:
        raise ValueError("message_type='question' requires question_meta")

    seq = await _next_seq(session, thread_id)

    message = Message(
        thread_id=thread_id,
        seq=seq,
        sender_type=sender_type,
        sender_id=sender_id,
        message_type=message_type,
        body=body,
        reply_to=reply_to,
        mentions=mentions if mentions is not None else [],
        question_meta=question_meta,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def answer_clears_awaiting(session: AsyncSession, message: Message) -> None:
    """If message.reply_to points at an awaiting question, clear its awaiting flag.

    question_meta is a plain JSON column (no MutableDict tracking) — never
    mutate the dict in place; build a new one and reassign so the ORM
    actually flags the row as dirty.
    """
    if message.reply_to is None:
        return

    result = await session.exec(select(Message).where(Message.id == message.reply_to))
    target = result.one_or_none()
    if target is None:
        return
    if target.message_type != "question":
        return
    if not target.question_meta or not target.question_meta.get("awaiting"):
        return

    target.question_meta = {**target.question_meta, "awaiting": False}
    session.add(target)
    await session.commit()


async def open_questions(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID | None = None,
    to: str | None = None,
) -> list[Message]:
    """List open (awaiting=True) questions, optionally filtered by thread and target."""
    stmt = select(Message).where(Message.message_type == "question")
    if thread_id is not None:
        stmt = stmt.where(Message.thread_id == thread_id)
    stmt = stmt.order_by(Message.created_at)

    result = await session.exec(stmt)
    messages = result.all()

    def is_open(m: Message) -> bool:
        if not m.question_meta or not m.question_meta.get("awaiting"):
            return False
        if to is not None and m.question_meta.get("to") != to:
            return False
        return True

    return [m for m in messages if is_open(m)]


async def last_task_activity(session: AsyncSession, task: Task, *, comm_v2: bool = False):
    """Dual-read last activity for a task (§8.1): the max of the latest
    TaskComment.created_at and the latest Message.created_at on the task's
    thread.

    `comm_v2` scopes the Message half of the read: threads carry system
    messages (dispatch briefing, waiting-resume "Antwort erhalten" lines) for
    EVERY task regardless of the assigned agent's comm_v2 status — those
    aren't comment-signal-equivalent activity for a non-pilot agent, and
    folding them in would silently shift stale-check timing for the whole
    fleet. Only comm_v2 agents actually drive the thread themselves, so only
    for them does Message.created_at count as real activity. Defaults to
    False (comment-only, byte-identical to the pre-dual-read behavior) —
    callers must explicitly pass `comm_v2=True` for pilot agents.
    """
    comment_result = await session.exec(
        select(func.max(TaskComment.created_at)).where(TaskComment.task_id == task.id)
    )
    latest_comment = comment_result.one()

    latest_message = None
    if comm_v2 and task.thread_id is not None:
        message_result = await session.exec(
            select(func.max(Message.created_at)).where(Message.thread_id == task.thread_id)
        )
        latest_message = message_result.one()

    candidates = [ensure_aware(t) for t in (latest_comment, latest_message) if t is not None]
    return max(candidates) if candidates else None


async def maybe_post_finish_nudge(session: AsyncSession, task: Task) -> None:
    """Post a system Nudge message once per fertig-signal episode, in place
    of the removed TaskComment resolution auto-promote (comm_v2 agents,
    §3.3 / §8.1).

    Dedupe: an "episode" runs from the agent's last Message on the thread to
    now. Skip if the nudge has already been posted within THIS episode (i.e.
    a nudge with this exact body exists after the last agent Message). Other
    system messages (e.g. the waiting-resume "▶ Antwort erhalten" line) may
    freely interleave without resetting the dedupe — only a new agent
    Message starts a fresh episode and allows the nudge to fire again.
    """
    thread = await ensure_task_thread(session, task)

    last_agent_result = await session.exec(
        select(Message)
        .where(Message.thread_id == thread.id, Message.sender_type == "agent")
        .order_by(Message.seq.desc())
        .limit(1)
    )
    last_agent_message = last_agent_result.first()
    episode_start_seq = last_agent_message.seq if last_agent_message is not None else 0

    existing_nudge_result = await session.exec(
        select(Message)
        .where(
            Message.thread_id == thread.id,
            Message.message_type == "system",
            Message.body == FINISH_NUDGE_BODY,
            Message.seq > episode_start_seq,
        )
        .limit(1)
    )
    if existing_nudge_result.first() is not None:
        return

    await post_message(
        session,
        thread_id=thread.id,
        sender_type="system",
        message_type="system",
        body=FINISH_NUDGE_BODY,
    )
