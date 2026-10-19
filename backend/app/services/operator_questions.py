"""Open operator questions of one agent — what the chat's question card reads.

An agent reaches the operator through two existing paths:

- ``mc question`` → a ``clarification_question`` approval; the task is
  ``blocked`` until the operator answers (``routers/approvals.py``).
- ``mc ask`` with the operator as target → a ``question`` message on the task
  thread; ``--blocking`` parks the task in ``waiting`` (``services/messaging.py``).

Some approvals are the agent asking, too (an install request, a blocker it
needs a decision on). Many others carry the agent's id but are MC asking
about the agent (watchdog escalations, dispatch gates, crash triage) — the
card says "your agent asks you", so those stay in the inbox. Which kinds
count is a registry below (ADR-093 §4), not "every pending approval".

This module folds the agent's questions into one list per agent, so the chat
shows the same card for every harness and runtime (Welle 3, E11). It reads
only; answering goes through the existing resolve paths
(``routers/agent_questions.py``). A question whose task already ended is no
card: nothing clears it when the task finishes, and it can no longer be
acted on.

Which approval kinds the chat may answer with one tap is a registry entry
too: a kind whose approval has consequences the card cannot state (installs,
cancelling a task) stays answerable in its own card and the chat links there.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.comm_constants import QUESTION_TARGETS
from app.models.approval import Approval
from app.models.task import Task
from app.models.thread import Thread
from app.services.blocker_triage import OPERATOR_ONLY_BLOCKER_TYPES
from app.services.messaging import open_questions
from app.utils import ensure_aware

# Approval kinds the AGENT raises (``mc question``, ``mc install-request``,
# ``mc blocked``) — the only ones the card shows. Everything else with an
# agent_id (lead_escalation, review_stuck, promote_approval, spawn_agent,
# x_post, …) is MC or another actor asking about the agent.
AGENT_RAISED_APPROVALS: frozenset[str] = frozenset({
    "clarification_question",
    "install_skill", "uninstall_skill",
    "install_plugin", "uninstall_plugin",
    "install_mcp", "uninstall_mcp",
    "blocker_decision",
})

# A blocker is the agent asking only when it needs the operator's decision or
# permission. A ``technical_problem`` is what the harness reports for a crash
# or timeout and what the lifecycle watchdog files for a silent agent; the
# comment fallback files ``other``. Those are reports, not questions.
AGENT_BLOCKER_TYPES: frozenset[str] = frozenset(OPERATOR_ONLY_BLOCKER_TYPES)

# A question of a task in one of these states can no longer be acted on.
FINISHED_TASK_STATUSES: frozenset[str] = frozenset({"done", "failed", "aborted", "cancelled"})

# Approval kinds the chat card answers itself: the answer is free text (or one
# of the agent's options) and resolving it only resumes the task.
CHAT_ANSWERABLE_APPROVALS: frozenset[str] = frozenset({"clarification_question"})

# Approval kinds that hold the agent's task until the operator acts.
BLOCKING_APPROVALS: frozenset[str] = frozenset({"clarification_question", "blocker_decision"})

# `mc ask --to <target>`: the first target is the operator (comm_constants);
# the others (the lead, another agent) are not operator questions.
OPERATOR_TARGET: str = QUESTION_TARGETS[0]

QuestionSource = Literal["approval", "thread"]


class OperatorQuestion(BaseModel):
    id: uuid.UUID
    source: QuestionSource
    kind: str
    answerable: bool
    question: str
    options: list[str] = []
    default: str | None = None
    blocking: bool
    task_id: uuid.UUID | None = None
    task_title: str | None = None
    created_at: datetime


def _str_options(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [o.strip() for o in value if isinstance(o, str) and o.strip()]


def _approval_text(approval: Approval) -> str:
    payload = approval.payload or {}
    for key in ("question", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (approval.description or "").strip()


def _is_agent_raised(approval: Approval) -> bool:
    if approval.action_type not in AGENT_RAISED_APPROVALS:
        return False
    if approval.action_type == "blocker_decision":
        return (approval.payload or {}).get("blocker_type") in AGENT_BLOCKER_TYPES
    return True


async def _tasks_by_id(session: AsyncSession, task_ids: set[uuid.UUID]) -> dict[uuid.UUID, Task]:
    if not task_ids:
        return {}
    rows = (await session.exec(select(Task).where(Task.id.in_(task_ids)))).all()  # type: ignore[union-attr]
    return {t.id: t for t in rows}


def _task_finished(task_id: uuid.UUID | None, tasks: dict[uuid.UUID, Task]) -> bool:
    task = tasks.get(task_id) if task_id is not None else None
    return task is not None and task.status in FINISHED_TASK_STATUSES


async def _approval_questions(session: AsyncSession, agent_id: uuid.UUID) -> list[OperatorQuestion]:
    rows = (
        await session.exec(
            select(Approval).where(
                Approval.agent_id == agent_id,
                Approval.status == "pending",
                Approval.action_type.in_(AGENT_RAISED_APPROVALS),  # type: ignore[attr-defined]
            )
        )
    ).all()
    rows = [a for a in rows if _is_agent_raised(a)]
    tasks = await _tasks_by_id(session, {a.task_id for a in rows if a.task_id is not None})
    out: list[OperatorQuestion] = []
    for a in rows:
        if _task_finished(a.task_id, tasks):
            continue
        payload = a.payload or {}
        title = payload.get("task_title")
        if not isinstance(title, str) and a.task_id in tasks:
            title = tasks[a.task_id].title
        answerable = a.action_type in CHAT_ANSWERABLE_APPROVALS
        out.append(OperatorQuestion(
            id=a.id,
            source="approval",
            kind=a.action_type,
            answerable=answerable,
            question=_approval_text(a),
            options=_str_options(payload.get("options")) if answerable else [],
            blocking=a.action_type in BLOCKING_APPROVALS,
            task_id=a.task_id,
            task_title=title if isinstance(title, str) else None,
            created_at=ensure_aware(a.created_at),
        ))
    return out


async def _thread_questions(session: AsyncSession, agent_id: uuid.UUID) -> list[OperatorQuestion]:
    # "Open" is messaging.open_questions' rule (awaiting + target); this only
    # narrows it to one agent and to task threads of tasks still running.
    messages = await open_questions(session, to=OPERATOR_TARGET, sender_id=agent_id)
    if not messages:
        return []
    threads = {
        t.id: t
        for t in (
            await session.exec(
                select(Thread).where(
                    Thread.id.in_({m.thread_id for m in messages}),  # type: ignore[union-attr]
                    Thread.kind == "task",
                    Thread.task_id.is_not(None),  # type: ignore[union-attr]
                )
            )
        ).all()
    }
    tasks = await _tasks_by_id(session, {t.task_id for t in threads.values() if t.task_id is not None})
    out: list[OperatorQuestion] = []
    for message in messages:
        thread = threads.get(message.thread_id)
        if thread is None or _task_finished(thread.task_id, tasks):
            continue
        meta = message.question_meta or {}
        task = tasks.get(thread.task_id)
        default = meta.get("default")
        out.append(OperatorQuestion(
            id=message.id,
            source="thread",
            kind="question",
            answerable=True,
            question=message.body.strip(),
            options=_str_options(meta.get("options")),
            default=default if isinstance(default, str) and default.strip() else None,
            blocking=bool(meta.get("blocking")),
            task_id=thread.task_id,
            task_title=task.title if task else None,
            created_at=ensure_aware(message.created_at),
        ))
    return out


async def open_operator_questions(session: AsyncSession, agent_id: uuid.UUID) -> list[OperatorQuestion]:
    """The agent's open questions to the operator: blocking ones first (they
    hold a task), then oldest first — the one the agent waits on longest."""
    questions = await _approval_questions(session, agent_id) + await _thread_questions(session, agent_id)
    return sorted(questions, key=lambda q: (not q.blocking, q.created_at))


async def find_open_question(
    session: AsyncSession, agent_id: uuid.UUID, question_id: uuid.UUID
) -> OperatorQuestion | None:
    for q in await open_operator_questions(session, agent_id):
        if q.id == question_id:
            return q
    return None
