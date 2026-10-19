"""Open operator questions of one agent — what the chat's question card reads.

An agent reaches the operator through two existing paths:

- ``mc question`` → a ``clarification_question`` approval; the task is
  ``blocked`` until the operator answers (``routers/approvals.py``).
- ``mc ask`` with the operator as target → a ``question`` message on the task
  thread; ``--blocking`` parks the task in ``waiting`` (``services/messaging.py``).

Every other pending approval of the agent (install requests, blockers, …) is
the agent asking, too. This module folds all of them into one list per agent,
so the chat shows the same card for every harness and runtime (Welle 3, E11).
It reads only; answering goes through the existing resolve paths
(``routers/agent_questions.py``).

Which approval kinds the chat may answer with one tap is a registry entry
below, not a branch: a kind whose approval has consequences the card cannot
state (installs, cancelling a task) stays answerable in its own card and the
chat links there.
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
from app.models.thread import Message, Thread
from app.utils import ensure_aware

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


async def _approval_questions(session: AsyncSession, agent_id: uuid.UUID) -> list[OperatorQuestion]:
    rows = (
        await session.exec(
            select(Approval).where(Approval.agent_id == agent_id, Approval.status == "pending")
        )
    ).all()
    out: list[OperatorQuestion] = []
    for a in rows:
        payload = a.payload or {}
        title = payload.get("task_title")
        if not isinstance(title, str) and a.task_id is not None:
            task = await session.get(Task, a.task_id)
            title = task.title if task else None
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
    rows = (
        await session.exec(
            select(Message, Thread)
            .join(Thread, Thread.id == Message.thread_id)
            .where(
                Message.message_type == "question",
                Message.sender_id == agent_id,
                Thread.kind == "task",
                Thread.task_id.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).all()
    out: list[OperatorQuestion] = []
    for message, thread in rows:
        meta = message.question_meta or {}
        if not meta.get("awaiting") or meta.get("to") != OPERATOR_TARGET:
            continue
        task = await session.get(Task, thread.task_id)
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
