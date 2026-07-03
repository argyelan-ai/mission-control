"""Multi-Agent Consensus Helper.

POST /api/v1/agent/consensus — Boss dispatches the same question to N agents,
waits for all of them, aggregates the results.

Uses the existing subtask/dispatch infrastructure:
1. Root task as a container (status=in_progress)
2. N subtasks — one per agent (auto-dispatch)
3. Watchdog detects phase completion → parent → review
4. Caller fetches results via GET /consensus/{id}
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_agent
from app.scopes import Scope, require_scope
from app.database import get_session
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from app.services.activity import emit_event

logger = logging.getLogger("mc.consensus")

router = APIRouter(prefix="/api/v1/agent", tags=["consensus"])


class ConsensusRequest(BaseModel):
    """Request for multi-agent consensus."""
    question: str
    agent_ids: list[uuid.UUID]
    board_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    timeout_minutes: int = 30

    @field_validator("agent_ids")
    @classmethod
    def validate_agent_count(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(v) < 2:
            raise ValueError("Mindestens 2 Agents fuer Konsens noetig")
        if len(v) > 6:
            raise ValueError("Maximal 6 Agents fuer Konsens")
        return v


class ConsensusResponse(BaseModel):
    consensus_id: str
    root_task_id: str
    subtask_ids: list[str]
    status: str  # "pending" | "partial" | "complete"
    agents: list[str]


@router.post(
    "/consensus",
    response_model=ConsensusResponse,
    dependencies=[Depends(require_scope(Scope.TASKS_CREATE))],
)
async def create_consensus(
    body: ConsensusRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_agent),
):
    """Create a consensus request: same question to N agents, parallel processing.

    The endpoint creates:
    1. A root task as a container
    2. N subtasks — one per agent with assigned_agent_id

    Watchdog detects phase completion automatically.
    """
    # Resolve board
    board_id = body.board_id
    if not board_id and agent.board_id:
        board_id = agent.board_id
    if not board_id:
        raise HTTPException(400, "board_id muss angegeben werden oder Agent muss einem Board zugewiesen sein")

    # Verify board exists
    board = await session.get(Board, board_id)
    if not board:
        raise HTTPException(404, f"Board {board_id} nicht gefunden")

    # Verify all agents exist
    target_agents: list[Agent] = []
    for aid in body.agent_ids:
        ag = await session.get(Agent, aid)
        if not ag:
            raise HTTPException(404, f"Agent {aid} nicht gefunden")
        target_agents.append(ag)

    agent_names = [a.name for a in target_agents]

    # 1. Root task (container)
    root_task = Task(
        board_id=board_id,
        title=f"Konsens: {body.question[:80]}",
        description=(
            f"## Konsens-Anfrage\n\n"
            f"**Frage:** {body.question}\n\n"
            f"**Agents:** {', '.join(agent_names)}\n\n"
            f"Erstellt von {agent.name}. "
            f"Wartet auf Antworten aller {len(target_agents)} Agents."
        ),
        status="in_progress",
        priority="medium",
        task_type="consensus",
        parent_task_id=body.parent_task_id,
        assigned_agent_id=agent.id,
        owner_agent_id=agent.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    session.add(root_task)
    await session.flush()  # generate ID

    # 2. Subtasks — one per agent
    subtask_ids: list[str] = []
    for target in target_agents:
        subtask = Task(
            board_id=board_id,
            title=f"Konsens-Beitrag: {body.question[:60]}",
            description=(
                f"## Konsens-Beitrag gefragt\n\n"
                f"**Frage:** {body.question}\n\n"
                f"Beantworte diese Frage so gut du kannst. "
                f"Dein Beitrag wird mit {len(target_agents) - 1} anderen Agents verglichen.\n\n"
                f"Schreibe deine Antwort als Kommentar und setze den Task auf `done`."
            ),
            status="inbox",
            priority="medium",
            task_type="consensus_subtask",
            parent_task_id=root_task.id,
            assigned_agent_id=target.id,
            owner_agent_id=agent.id,
            callback_agent_id=agent.id,
            dispatch_intent="subtask",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(subtask)
        await session.flush()
        subtask_ids.append(str(subtask.id))

        # Dispatch in background
        background_tasks.add_task(_dispatch_consensus_subtask, subtask.id, board_id)

    await session.commit()

    # Activity event
    await emit_event(
        session,
        event_type="consensus.created",
        title=f"Konsens-Anfrage: {body.question[:60]}",
        agent_id=agent.id,
        board_id=board_id,
        detail={
            "consensus_id": str(root_task.id),
            "question": body.question,
            "agents": agent_names,
            "subtask_count": len(subtask_ids),
        },
    )

    return ConsensusResponse(
        consensus_id=str(root_task.id),
        root_task_id=str(root_task.id),
        subtask_ids=subtask_ids,
        status="pending",
        agents=agent_names,
    )


@router.get(
    "/consensus/{consensus_id}",
    dependencies=[Depends(require_scope(Scope.TASKS_READ))],
)
async def get_consensus_status(
    consensus_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    agent: Agent = Depends(require_agent),
):
    """Fetch status and results of a consensus request."""
    root_task = await session.get(Task, consensus_id)
    if not root_task:
        raise HTTPException(404, "Konsens-Task nicht gefunden")

    # Load subtasks
    result = await session.exec(
        select(Task).where(Task.parent_task_id == consensus_id)
    )
    subtasks = result.all()

    # Compute status
    done_count = sum(1 for s in subtasks if s.status == "done")
    total_count = len(subtasks)

    if done_count == total_count and total_count > 0:
        consensus_status = "complete"
    elif done_count > 0:
        consensus_status = "partial"
    else:
        consensus_status = "pending"

    # Collect responses (last comments of the done subtasks)
    responses: list[dict] = []
    for subtask in subtasks:
        # Fetch the last comment from the assigned agent
        comment_result = await session.exec(
            select(TaskComment)
            .where(TaskComment.task_id == subtask.id)
            .where(TaskComment.author_agent_id == subtask.assigned_agent_id)
            .order_by(col(TaskComment.created_at).desc())
            .limit(1)
        )
        last_comment = comment_result.first()

        agent_record = await session.get(Agent, subtask.assigned_agent_id) if subtask.assigned_agent_id else None

        responses.append({
            "subtask_id": str(subtask.id),
            "agent_id": str(subtask.assigned_agent_id) if subtask.assigned_agent_id else None,
            "agent_name": agent_record.name if agent_record else "Unbekannt",
            "status": subtask.status,
            "response": last_comment.content if last_comment else None,
            "completed_at": subtask.completed_at.isoformat() if subtask.completed_at else None,
        })

    return {
        "consensus_id": str(consensus_id),
        "question": root_task.description,
        "status": consensus_status,
        "total": total_count,
        "done": done_count,
        "responses": responses,
        "root_task_status": root_task.status,
    }


async def _dispatch_consensus_subtask(task_id: uuid.UUID, board_id: uuid.UUID):
    """Background dispatch of a consensus subtask via auto_dispatch_task."""
    try:
        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task_id, board_id)
    except Exception as e:
        logger.error("Consensus subtask dispatch failed for %s: %s", task_id, e)
