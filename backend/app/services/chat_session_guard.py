"""A chat turn never works a task of its own agent.

Round-1 user test t1 (22.10.2026, MUST-1): an omp agent runs its chat in a
headless ACP session (``docker/omp-bridge/acp_chat.py``) and every task in a
SEPARATE session the bridge opens. The operator asked "how far are you?" in
the chat while a task was running. The chat session found the task with
``mc me``, could not see the run's files (it looked in its own workspace, not
in ``/deliverables/<task>/``), took the task for "not started" and did the
whole task a second time: a second ``index.html`` with other colours, a
progress comment AFTER the task had already gone to review, a second
``mc finish``. The operator then saw two different truths.

Two layers stop that:

* the chat turn is told what the run is doing (``chat_run_context``);
* the server refuses every write to the run state of such a task when the
  call comes from the chat session — this module.

How the server knows: the chat daemon starts its ``omp acp`` child with
``MC_SESSION_ROLE=chat``, and the ``mc`` CLI sends that as
``X-MC-Session-Role: chat`` (``scripts/mc-cli/mc_cli/client.py``). The run
session never carries it. It is a marker against an honest mistake of the
model, not an authentication: the token is the same agent's token either way.

Which tasks (round-2 review, 23.10.2026 — "no gaps, not too wide"): the
tasks a run of this agent holds right now, or let go of within
``chat_run_context.RECENT_WINDOW`` — the same set the chat's context block
names. A run holds a task while it is assigned to the agent and live
(``in_progress`` / ``waiting`` / ``blocked`` / ``user_test``, or delivered to
it in the inbox) — and a reviewer's run holds the ``review`` card a review
handoff gave it, for as long as it stays there (``review_run_holds``). A finished task stays covered for 30 minutes after the run
let go when this agent worked it (an ``→ in_progress`` event with its id) — a
task handed to human review loses its assignee, and the 22:22:22 comment of
the incident landed on exactly such one, 36 s after the handoff. Not covered:
a task whose run ended long ago (the operator asks about it in its thread;
round-1 had no time limit), and a task nobody runs yet — the one the chat just
created for itself (inbox, not delivered). Tasks of other agents are not this
guard's business; the existing board and scope rules apply to them unchanged.

What: every write to the run state — also ``mc review`` / ``mc review-note``
on the card its review run works (round-3 r1 S1: the chat could approve the
review its own review run was still doing). A plain reply (``message``
comment, ``CHAT_REPLY_COMMENT_TYPES``) is not run state — the chat may answer
in the task thread; the comment route lets it through, skips the ACK
handshake for it and stores it as ``chat_reply`` (``comment_types``), which no
watchdog counts as the run's sign of life (round-3 r1 S3). ``mc msg`` (both
message routes) never ACKs from the chat either (round-3 r1 S2). Not on a card its review run works: there any reviewer comment, a
``message`` too, ends the review wait (``review_park``) and passes the
"reviewer said nothing" check (``proof_gate``), so a chat reply would speak
for the review run. Each refusal leaves one timeline row per task and action per
``REFUSAL_EVENT_EVERY`` however often the model retries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskEvent
from app.services.chat_run_context import (
    FINISHED_STATUSES,
    LIVE_STATUSES,
    RECENT_WINDOW,
    review_run_holds,
)

logger = logging.getLogger(__name__)

#: The header the ``mc`` CLI sets from ``MC_SESSION_ROLE`` (twin:
#: ``scripts/mc-cli/mc_cli/client.py``).
SESSION_ROLE_HEADER = "X-MC-Session-Role"
#: The only role the server acts on. Anything else (or nothing) is a run
#: session or an older CLI — byte-identical behaviour to before.
ROLE_CHAT = "chat"
#: Machine-readable start of the refusal, for the agent and for tests.
REFUSAL_CODE = "chat_session_task_write"
#: The activity event of a refusal (task timeline, kind "stuck").
REFUSAL_EVENT = "task.chat_write_rejected"
#: At most one timeline row per task and action in this span (a model that
#: retries the refused call must not fill the operator's timeline).
REFUSAL_EVENT_EVERY = timedelta(minutes=10)
#: Comment types the chat may write on a task of its own run: an answer, not
#: run state. Everything else (progress, resolution, evidence, blocker,
#: reflection, handoff, …) moves the run and stays the run's.
CHAT_REPLY_COMMENT_TYPES = frozenset({"message"})


def is_chat_session(request: Request | None) -> bool:
    """True when the call comes from an agent's chat session."""
    if request is None:
        return False
    return (request.headers.get(SESSION_ROLE_HEADER) or "").strip().lower() == ROLE_CHAT


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _worked_by(session: AsyncSession, task: Task, agent: Agent) -> bool:
    """``agent`` ran this task once (an ``→ in_progress`` event with its id)."""
    row = (
        await session.exec(
            select(TaskEvent.id).where(
                TaskEvent.task_id == task.id,
                TaskEvent.agent_id == agent.id,
                TaskEvent.to_status == "in_progress",
            ).limit(1)
        )
    ).first()
    return row is not None


async def _run_let_go_at(session: AsyncSession, task: Task) -> datetime | None:
    """When the last run let go of the task: its last ``in_progress → …``
    event; without one (a status set around the lifecycle) the task's own
    ``completed_at`` / ``updated_at``."""
    left = (
        await session.exec(
            select(func.max(TaskEvent.created_at)).where(
                TaskEvent.task_id == task.id, TaskEvent.from_status == "in_progress",
            )
        )
    ).first()
    return _aware(left or task.completed_at or task.updated_at)


async def task_is_agents_run(
    session: AsyncSession, task: Task, agent: Agent, *, now: datetime | None = None,
) -> bool:
    """A run of ``agent`` holds the task now, or let go of it within
    ``RECENT_WINDOW`` (see the module docstring for why exactly these)."""
    status = task.status
    if review_run_holds(task, agent):
        # The card its review run works: live for the reviewer, no time limit.
        return True
    delivered = status == "inbox" and task.dispatched_at is not None
    if status in LIVE_STATUSES or delivered:
        if task.assigned_agent_id == agent.id:
            return True
        return task.assigned_agent_id is None and await _worked_by(session, task, agent)
    if status not in FINISHED_STATUSES:
        # inbox, not delivered: no run holds it — e.g. the task the chat just
        # created for itself (round-2 r1 NICE-5).
        return False
    if task.assigned_agent_id != agent.id and not await _worked_by(session, task, agent):
        return False
    let_go = await _run_let_go_at(session, task)
    now = _aware(now) or datetime.now(timezone.utc)
    return let_go is not None and let_go >= now - RECENT_WINDOW


async def _refusal_already_shown(
    session: AsyncSession, task: Task, agent: Agent, action: str,
) -> bool:
    """A refusal row for this task, agent and action is younger than
    ``REFUSAL_EVENT_EVERY``."""
    from app.models.activity import ActivityEvent

    since = datetime.now(timezone.utc) - REFUSAL_EVENT_EVERY
    details = (
        await session.exec(
            select(ActivityEvent.detail).where(
                ActivityEvent.event_type == REFUSAL_EVENT,
                ActivityEvent.task_id == task.id,
                ActivityEvent.agent_id == agent.id,
                ActivityEvent.created_at >= since,
            ).limit(50)
        )
    ).all()
    return any(isinstance(d, dict) and d.get("action") == action for d in details)


def refusal_detail(task: Task, action: str) -> str:
    """The text the agent reads in the chat (``mc`` prints the HTTP detail).
    English — the agents' working language (operator decision 23.09.2026);
    the code first, so a test and a log grep find it."""
    return (
        f"{REFUSAL_CODE}: task '{task.title}' ({task.id}) belongs to its own run, "
        f"not to this chat — `{action}` from the chat is refused. In the chat, only "
        "report on it (read: `mc task-get <id>`, the files under "
        f"/deliverables/{task.id}/). Do not change the task; if the operator wants a "
        "change, they make it in the task view."
    )


async def refuse_chat_task_write(
    session: AsyncSession,
    request: Request | None,
    agent: Agent,
    task: Task,
    action: str,
) -> None:
    """Raise 409 when a chat session writes the run state of a task of its
    own agent. No-op for every other caller. ``action`` names the write in the
    refusal and the activity event (``comment progress``, ``status review``,
    ``deliverable``, ``checklist``, ``recover``, ``question``, ``help``,
    ``delegate``, ``review approve``, ``review-note hold``, …)."""
    # rule: R-chat-never-works-own-run - a chat question once ran a task a second time
    if not is_chat_session(request):
        return
    if not await task_is_agents_run(session, task, agent):
        return
    detail: dict[str, Any] = {"action": action, "status": task.status}
    logger.warning(
        "chat session write refused: agent=%s task=%s action=%s status=%s",
        agent.name, task.id, action, task.status,
    )
    try:
        if not await _refusal_already_shown(session, task, agent, action):
            from app.services.activity import emit_event

            await emit_event(
                session, REFUSAL_EVENT,
                # Short: the row sits in this task's own timeline, the agent is
                # its actor chip — a long title truncates on a phone.
                f"Change from the chat refused: {action}",
                severity="warning",
                board_id=task.board_id, task_id=task.id, agent_id=agent.id,
                detail=detail,
            )
    except Exception:  # noqa: BLE001 — the audit line must never turn the 409 into a 500
        logger.warning("chat_write_rejected event failed", exc_info=True)
    raise HTTPException(status_code=409, detail=refusal_detail(task, action))
