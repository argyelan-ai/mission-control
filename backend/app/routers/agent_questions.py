"""The chat's question card for every agent (Welle 3, E11; ADR-090 P3).

``GET  /agents/{id}/chat/questions`` — the agent's open questions to the
operator (``services/operator_questions.py``), for any harness and runtime.
``POST /agents/{id}/chat/questions/{question_id}/answer`` — one answer, sent
through the path that already owns that kind of question, so the task resumes
and the agent receives the answer exactly as from the inbox or task detail:

- a ``clarification_question`` approval → ``PATCH /approvals/{id}`` (approved,
  the answer as ``resolver_note``): task ``blocked`` → ``in_progress`` plus a
  ``resolution`` comment the agent's pull delivers;
- a thread question (``mc ask``) → ``POST /tasks/{id}/thread/messages`` with
  ``reply_to``: the awaiting flag clears and a ``waiting`` task resumes.

Reading is open to every signed-in user (the card renders read-only);
answering needs the admin role, like every other chat control.

ACP elicitations (omp's own ask tool) keep their card in the timeline
(``/chat/elicitation/{id}``): they live in the chat daemon, not in MC's DB.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.task import Task
from app.services.operator_questions import (
    OperatorQuestion,
    find_open_question,
    open_operator_questions,
)

router = APIRouter(prefix="/api/v1", tags=["agent-chat"])

MAX_ANSWER_CHARS = 4000


class QuestionList(BaseModel):
    questions: list[OperatorQuestion]


class QuestionAnswerBody(BaseModel):
    answer: str

    @field_validator("answer")
    @classmethod
    def _answer_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("answer must not be empty")
        if len(v) > MAX_ANSWER_CHARS:
            raise ValueError(f"answer longer than {MAX_ANSWER_CHARS} characters")
        return v


async def _agent_or_404(agent_id: uuid.UUID, session: AsyncSession) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("/agents/{agent_id}/chat/questions", response_model=QuestionList)
async def get_agent_questions(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Open questions of this agent to the operator — blocking first, then
    oldest first. Empty list when nothing waits."""
    await _agent_or_404(agent_id, session)
    return QuestionList(questions=await open_operator_questions(session, agent_id))


@router.post("/agents/{agent_id}/chat/questions/{question_id}/answer")
async def post_agent_question_answer(
    agent_id: uuid.UUID,
    question_id: uuid.UUID,
    body: QuestionAnswerBody,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Answers one open question. 200 ``{"status": "answered", "task_status"}``;
    409 ``{"reason": "question_not_open"}`` when it was answered elsewhere
    meanwhile (inbox, another device); 409 ``{"reason": "answer_elsewhere"}``
    for an approval kind the chat does not answer itself (it links to its
    own card, which states the consequences)."""
    await _agent_or_404(agent_id, session)
    question = await find_open_question(session, agent_id, question_id)
    if question is None:
        return JSONResponse(status_code=409, content={"reason": "question_not_open"})
    if not question.answerable:
        return JSONResponse(status_code=409, content={"reason": "answer_elsewhere"})

    # The owning endpoints are called as functions, not re-implemented: the
    # resume, the comment for the agent and the activity events stay in one
    # place each.
    if question.source == "approval":
        from app.routers.approvals import ApprovalResolve, resolve_approval

        await resolve_approval(
            question.id,
            ApprovalResolve(status="approved", resolver_note=body.answer),
            session=session,
            current_user=current_user,
        )
    else:
        from app.routers.tasks import ThreadMessageCreate, post_thread_message

        await post_thread_message(
            question.task_id,
            ThreadMessageCreate(body=body.answer, reply_to=question.id),
            session=session,
            current_user=current_user,
        )

    task_status = None
    if question.task_id is not None:
        session.expire_all()
        task = await session.get(Task, question.task_id)
        task_status = task.status if task else None
    return {"status": "answered", "task_status": task_status}
