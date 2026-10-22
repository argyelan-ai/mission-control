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

Which tasks: every task assigned to this agent or worked by it before (an
``→ in_progress`` event with its id — a task handed to human review loses its
assignee, the 22:22:22 comment of the incident landed on exactly such one).
Tasks of other agents are not this guard's business; the existing board and
scope rules apply to them unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskEvent

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


def is_chat_session(request: Request | None) -> bool:
    """True when the call comes from an agent's chat session."""
    if request is None:
        return False
    return (request.headers.get(SESSION_ROLE_HEADER) or "").strip().lower() == ROLE_CHAT


async def task_is_agents_run(session: AsyncSession, task: Task, agent: Agent) -> bool:
    """The task belongs to a run of ``agent``: assigned to it, or worked by
    it before (it has an ``→ in_progress`` event with ``agent.id``)."""
    if task.assigned_agent_id == agent.id:
        return True
    worked = (
        await session.exec(
            select(TaskEvent.id).where(
                TaskEvent.task_id == task.id,
                TaskEvent.agent_id == agent.id,
                TaskEvent.to_status == "in_progress",
            ).limit(1)
        )
    ).first()
    return worked is not None


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
    ``deliverable``, ``checklist``, ``recover``)."""
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
