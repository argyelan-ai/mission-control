"""
Research Router — Task-basierte Recherche mit AI Agent.

User beschreibt Thema → Research-Task wird erstellt + an Agent dispatcht →
Agent schreibt Antwort via TaskComment → User speichert Ergebnis in Knowledge Base.

Phase 29 breaking change: research/content task creation is now ASYNC.
Callers must poll GET /api/v1/tasks/{id} to check completion (status `done`)
and then GET /api/v1/research/{project_id}/chat for the agent's reply.
Frontend will be updated in Phase 31.
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import engine, get_session
from app.models.agent import Agent
from app.models.board import PlannerMessage, Project
from app.models.memory import BoardMemory
from app.models.task import Task as TaskModel
from app.services.activity import emit_event
from app.services.dispatch import auto_dispatch_task
from app.services.sse import broadcast
from app.redis_client import RedisKeys

logger = logging.getLogger("research")
router = APIRouter(prefix="/api/v1/research", tags=["research"])

# ── System Prompt ───────────────────────────────────────────────────────────

RESEARCH_SYSTEM_PROMPT = """Du bist jetzt im Research-Modus. Der User gibt dir ein Thema zur Recherche.
Deine Aufgabe:
1. Verstehe das Thema und den Kontext — stelle Rueckfragen wenn noetig
2. Recherchiere gruendlich und strukturiert
3. Liefere ein umfassendes Ergebnis im folgenden Format:

## Zusammenfassung
[2-3 Saetze: Was wurde recherchiert und was ist das Kernergebnis]

## Ergebnisse
[Strukturierte Darstellung der Recherche-Ergebnisse mit Unterpunkten]

## Empfehlung
[Konkrete Handlungsempfehlung basierend auf den Ergebnissen]

## Quellen & Referenzen
[Wenn vorhanden: Links, Dokumentationen, Vergleiche]

Halte die Recherche objektiv und faktenbasiert. Markiere Unsicherheiten klar."""


# ── Request Models ──────────────────────────────────────────────────────────

class ResearchStartRequest(BaseModel):
    title: str
    description: str | None = None
    board_id: str
    initial_message: str | None = None


class ResearchMessageRequest(BaseModel):
    content: str


class ResearchSaveRequest(BaseModel):
    title: str | None = None
    content: str | None = None  # Override: eigener Content statt letzte Agent-Nachricht
    tags: list[str] = []
    agent_id: str | None = None  # Optional: Agent-scoped speichern


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("")
async def list_research(
    board_id: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Alle Research-Projekte auflisten."""
    query = (
        select(Project)
        .where(Project.project_type == "research")
        .order_by(Project.created_at.desc())  # type: ignore[union-attr]
    )
    if board_id:
        query = query.where(Project.board_id == uuid.UUID(board_id))
    result = await session.exec(query)
    return result.all()


@router.post("/start")
async def start_research(
    body: ResearchStartRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Neue Recherche starten: Projekt + Chat-Session erstellen."""
    # Projekt als Research-Typ erstellen
    project = Project(
        board_id=uuid.UUID(body.board_id),
        name=body.title,
        description=body.description,
        project_type="research",
        status="planning",
        created_by="research",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # System-Prompt speichern
    system_msg = PlannerMessage(
        project_id=project.id,
        role="system",
        content=RESEARCH_SYSTEM_PROMPT,
    )
    session.add(system_msg)

    # Initiale User-Nachricht
    initial_content = body.initial_message or body.description or body.title
    user_msg = PlannerMessage(
        project_id=project.id,
        role="user",
        content=initial_content,
    )
    session.add(user_msg)
    await session.commit()

    # Activity Event
    await emit_event(
        session,
        "research.started",
        f"Neue Recherche gestartet: {project.name}",
        board_id=project.board_id,
        detail={"project_id": str(project.id)},
    )

    # Phase 29: Research-Task erstellen und via auto_dispatch_task ausliefern.
    # Vorher: gateway chat send + poll reply (synchron). Jetzt asynchron —
    # Agent schreibt Antwort als TaskComment, Caller pollt GET /tasks/{id}.
    research_agent = await _find_research_agent(session, uuid.UUID(body.board_id))

    research_board_task = None
    if research_agent:
        from app.utils import utcnow
        research_board_task = TaskModel(
            board_id=uuid.UUID(body.board_id),
            project_id=project.id,
            title=f"Research: {body.title}",
            description=(
                f"[Research-Modus — Thema: {project.name}]\n\n{initial_content}"
            ),
            status="inbox",
            priority="medium",
            assigned_agent_id=research_agent.id,
            is_auto_created=True,
            auto_reason="Research-Session gestartet",
        )
        session.add(research_board_task)
        await session.commit()
        await session.refresh(research_board_task)

        # Async dispatch — endpoint returns immediately
        asyncio.create_task(
            auto_dispatch_task(research_board_task.id, research_board_task.board_id)
        )

    return {
        "project": project,
        "research_agent": {
            "id": str(research_agent.id),
            "name": research_agent.name,
            "emoji": research_agent.emoji,
        } if research_agent else None,
        "task_id": str(research_board_task.id) if research_board_task else None,
        "status": "dispatched" if research_board_task else "no_agent",
    }


@router.get("/{project_id}/chat")
async def get_research_chat(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Chat-History einer Recherche."""
    result = await session.exec(
        select(PlannerMessage)
        .where(PlannerMessage.project_id == project_id)
        .order_by(PlannerMessage.created_at)  # type: ignore[union-attr]
    )
    messages = result.all()
    return [m for m in messages if m.role != "system"]


@router.post("/{project_id}/message")
async def send_research_message(
    project_id: uuid.UUID,
    body: ResearchMessageRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """User-Nachricht an den Research-Agent senden."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Recherche nicht gefunden")

    # User-Nachricht speichern
    user_msg = PlannerMessage(
        project_id=project_id,
        role="user",
        content=body.content,
    )
    session.add(user_msg)
    await session.commit()
    await session.refresh(user_msg)

    # Phase 29: Folge-Nachricht als neuer Research-Task an den Research-Agent.
    # Async dispatch — Caller pollt /tasks/{id}.
    research_agent = await _find_research_agent(session, project.board_id)
    if research_agent:
        followup_task = TaskModel(
            board_id=project.board_id,
            project_id=project_id,
            title=f"Research: {body.content[:80]}",
            description=body.content,
            status="inbox",
            priority="medium",
            assigned_agent_id=research_agent.id,
            is_auto_created=True,
            auto_reason="Research follow-up message",
        )
        session.add(followup_task)
        await session.commit()
        await session.refresh(followup_task)
        asyncio.create_task(
            auto_dispatch_task(followup_task.id, followup_task.board_id)
        )

    return user_msg


@router.post("/{project_id}/save")
async def save_research(
    project_id: uuid.UUID,
    body: ResearchSaveRequest,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Recherche-Ergebnis in Knowledge Base speichern."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Recherche nicht gefunden")

    # Content: entweder manuell oder letzte Agent-Nachricht
    save_content = body.content
    if not save_content:
        result = await session.exec(
            select(PlannerMessage)
            .where(PlannerMessage.project_id == project_id, PlannerMessage.role == "assistant")
            .order_by(PlannerMessage.created_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
        last_reply = result.first()
        if last_reply:
            save_content = last_reply.content

    if not save_content:
        raise HTTPException(400, "Kein Recherche-Ergebnis vorhanden. Bitte zuerst mit dem Agent chatten.")

    # In Knowledge Base speichern
    save_title = body.title or project.name
    entry = BoardMemory(
        board_id=project.board_id,
        agent_id=uuid.UUID(body.agent_id) if body.agent_id else None,
        title=save_title,
        content=save_content,
        tags=body.tags if body.tags else ["research"],
        source="research",
        memory_type="research",
        is_pinned=False,
        auto_generated=False,
    )
    session.add(entry)

    # Projekt als "done" markieren und Plan-Summary speichern
    project.plan_summary = save_content
    project.status = "done"
    session.add(project)

    await session.commit()
    await session.refresh(entry)
    await session.refresh(project)

    try:
        from app.services.memory_indexing import index_memory
        await index_memory(entry)
    except Exception as e:
        logger.warning("save_research index failed: %s", e)

    # Activity Event
    await emit_event(
        session,
        "research.completed",
        f"Recherche abgeschlossen: {save_title}",
        board_id=project.board_id,
        detail={
            "project_id": str(project.id),
            "knowledge_id": str(entry.id),
        },
    )

    # SSE broadcast
    await broadcast(
        RedisKeys.agents_events(),
        "research.completed",
        {
            "project_id": str(project.id),
            "knowledge_id": str(entry.id),
            "title": save_title,
        },
    )

    return {
        "project": project,
        "knowledge_entry": entry,
    }


@router.delete("/{project_id}", status_code=204)
async def delete_research(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    """Recherche und zugehoerige Chat-Messages loeschen."""
    project = await session.get(Project, project_id)
    if not project or project.project_type != "research":
        raise HTTPException(404, "Recherche nicht gefunden")

    # Chat-Messages löschen
    messages = await session.exec(
        select(PlannerMessage).where(PlannerMessage.project_id == project_id)
    )
    for msg in messages.all():
        await session.delete(msg)

    await session.delete(project)
    await session.commit()


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _find_research_agent(session: AsyncSession, board_id: uuid.UUID) -> Agent | None:
    """Research-Agent finden: Researcher-named bevorzugt, dann Board Lead, dann erster Agent.

    Phase 29: gateway_agent_id ist nicht mehr erforderlich — auto_dispatch_task
    routet runtime-agnostisch (cli-bridge / host / claude-code).
    """
    result = await session.exec(
        select(Agent).where(Agent.board_id == board_id)
    )
    agents = list(result.all())

    # Researcher-named bevorzugen
    for agent in agents:
        if "research" in (agent.name or "").lower():
            return agent

    # Board Lead bevorzugen
    for agent in agents:
        if agent.is_board_lead:
            return agent

    # Fallback: erster Agent
    return agents[0] if agents else None
