"""Which threads an agent may speak in — one truth, used in both directions.

This used to live as ``_message_threads_for_agent`` inside ``routers/agents``,
where only the *delivery* path could reach it. That asymmetry was the bug:
MC handed Mark's general-chat message to Boss and Boss acked it, but the only
write endpoint an agent had (``POST /tasks/current/messages``) was bound to
``agent.current_task_id``. With no active task it answered 409, and even with
one it would have written into the *task* thread rather than the conversation
the question came from. The general chat was a one-way street — measured live
on 2026-07-29: thread 8015c75e carried five operator messages and not a single
agent reply, while ``last_acked_seq`` proved delivery worked.

So the scope rule now lives here and is imported by both sides:

  * delivery  — ``routers/agents`` (``/me/poll``, ``/me/inbox``)
  * reply     — ``routers/agent_scoped`` (``POST /threads/{id}/messages``)

**An agent may answer exactly where it may listen.** Not a new permission —
the same one, read twice. A second, hand-copied rule is how the two drift
apart, and a reply path that is wider than the delivery path would let an
agent post into conversations it cannot even see.
"""
from __future__ import annotations

import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task
from app.models.thread import Thread

# Task statuses whose task-thread messages are eligible for delivery — mirror
# the comment path's active set so the message and comment views stay aligned.
# `waiting` MUST be in this list (live pilot finding 2026-07-20): a task
# parked on a blocking ask is the one state that exists BECAUSE a message
# (the answer) is expected — without it, anything posted to the thread while
# the task waits (status updates, "moment, noch was" operator notes, the
# answer itself if the resume ever decouples from posting) is silently
# withheld from the agent until the status flips.
MESSAGE_ACTIVE_STATUSES = [
    "in_progress", "inbox", "review", "blocked", "done", "user_test", "waiting",
]


async def message_threads_for_agent(agent: Agent, session: AsyncSession) -> list:
    """``[(thread, task|None)]`` — the threads this agent takes part in.

    The task is carried along because the delivery path needs its status for
    first-cursor fast-forwarding; the reply path ignores it.
    """
    active_res = await session.exec(
        select(Task).where(
            Task.assigned_agent_id == agent.id,
            Task.status.in_(MESSAGE_ACTIVE_STATUSES),  # type: ignore[union-attr]
        )
    )
    tasks_by_thread = {
        t.thread_id: t for t in active_res.all() if t.thread_id is not None
    }
    # DM thread of this agent (Mark <-> agent, no task). Second tuple entry is
    # None: no task, hence no done/failed fast-forward — a DM has no "finished
    # history" that may be skipped.
    dm_res = await session.exec(
        select(Thread).where(Thread.kind == "dm", Thread.agent_id == agent.id)
    )
    dm_pairs = [(th, None) for th in dm_res.all()]
    if not tasks_by_thread:
        return dm_pairs
    threads_res = await session.exec(
        select(Thread).where(Thread.id.in_(tasks_by_thread.keys()))  # type: ignore[union-attr]
    )
    return [(th, tasks_by_thread[th.id]) for th in threads_res.all()] + dm_pairs


async def thread_agent_may_write_to(
    session: AsyncSession, agent: Agent, thread_id: uuid.UUID
) -> Thread | None:
    """The thread, if this agent takes part in it — otherwise None.

    Deliberately does not distinguish "does not exist" from "not yours": the
    caller answers 404 for both, so an agent cannot probe for the existence of
    conversations it has no part in.
    """
    for thread, _task in await message_threads_for_agent(agent, session):
        if thread.id == thread_id:
            return thread
    return None
