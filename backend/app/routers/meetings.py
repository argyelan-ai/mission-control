"""
Meetings router — agent meetings + direct messages.

Static paths (/stream, /messages) before parameterized ones (/{id}).
"""
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_role, require_user
from app.database import get_session
from app.models.meeting import AgentMeeting, AgentMeetingMessage, AgentMessage
from app.redis_client import RedisKeys
from app.services.meeting_service import (
    MeetingAlreadyRunningError,
    MeetingError,
    MeetingNotFoundError,
    MeetingStateError,
    cancel_meeting,
    start_meeting,
)
from app.services.sse import make_sse_response

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])


# ── Pydantic Schemas ────────────────────────────────────────────────────


class MeetingCreate(BaseModel):
    board_id: uuid.UUID
    title: str
    agenda: list[str] = Field(min_length=1)
    meeting_type: Literal["weekly", "ad_hoc", "retrospective"] = "ad_hoc"
    participant_ids: list[uuid.UUID] | None = None


class MeetingResponse(BaseModel):
    id: uuid.UUID
    board_id: uuid.UUID
    title: str
    meeting_type: str
    status: str
    agenda: list[str] | None = None
    participant_ids: list[str] | None = None
    summary: str | None = None
    decisions: list[dict[str, Any]] | None = None
    action_items: list[dict[str, Any]] | None = None
    memory_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MeetingMessageResponse(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    agent_name: str | None = None
    role: str
    content: str
    round: int
    topic_index: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ── SSE Stream (static — before parameterized routes) ─────────────────


@router.get("/stream")
async def meeting_stream(
    _user=Depends(require_user),
):
    """SSE stream for live meeting updates."""
    return make_sse_response([RedisKeys.meeting_events()])


# ── Meeting CRUD ─────────────────────────────────────────────────────────


@router.get("", response_model=list[MeetingResponse])
async def list_meetings(
    board_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    _user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """List meetings — optionally filter by board and status."""
    stmt = select(AgentMeeting).order_by(AgentMeeting.created_at.desc()).limit(limit)
    if board_id:
        stmt = stmt.where(AgentMeeting.board_id == board_id)
    if status:
        stmt = stmt.where(AgentMeeting.status == status)
    result = await session.exec(stmt)
    return result.all()


@router.post("", response_model=MeetingResponse, status_code=201)
async def create_meeting(
    body: MeetingCreate,
    _user=Depends(require_role("operator")),
    session: AsyncSession = Depends(get_session),
):
    """Create a meeting and start it immediately."""
    try:
        meeting = await start_meeting(
            session,
            board_id=body.board_id,
            title=body.title,
            agenda=body.agenda,
            meeting_type=body.meeting_type,
            participant_ids=body.participant_ids,
        )
        return meeting
    except MeetingAlreadyRunningError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MeetingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(
    meeting_id: uuid.UUID,
    _user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Fetch meeting details."""
    meeting = await session.get(AgentMeeting, meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting nicht gefunden")
    return meeting


@router.get("/{meeting_id}/messages", response_model=list[MeetingMessageResponse])
async def get_meeting_messages(
    meeting_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Fetch meeting messages (paginated)."""
    stmt = (
        select(AgentMeetingMessage)
        .where(AgentMeetingMessage.meeting_id == meeting_id)
        .order_by(AgentMeetingMessage.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.exec(stmt)
    return result.all()


@router.post("/{meeting_id}/cancel", response_model=MeetingResponse)
async def cancel_meeting_endpoint(
    meeting_id: uuid.UUID,
    _user=Depends(require_role("operator")),
    session: AsyncSession = Depends(get_session),
):
    """Cancel a running meeting."""
    try:
        meeting = await cancel_meeting(session, meeting_id)
        return meeting
    except MeetingNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MeetingStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ── Agent Direct Messages ──────────────────────────────────────────────


class AgentMessageCreate(BaseModel):
    to_agent_id: uuid.UUID
    content: str = Field(min_length=1, max_length=5000)
    thread_id: uuid.UUID | None = None
    reply_to_id: uuid.UUID | None = None


class AgentMessageResponse(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    from_agent_id: uuid.UUID
    to_agent_id: uuid.UUID
    content: str
    status: str
    reply_to_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/agent-messages", response_model=list[AgentMessageResponse])
async def list_agent_messages(
    from_agent_id: uuid.UUID | None = Query(default=None),
    to_agent_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """List all agent direct messages (user auth)."""
    stmt = select(AgentMessage).order_by(AgentMessage.created_at.desc()).limit(limit)
    if from_agent_id:
        stmt = stmt.where(AgentMessage.from_agent_id == from_agent_id)
    if to_agent_id:
        stmt = stmt.where(AgentMessage.to_agent_id == to_agent_id)
    result = await session.exec(stmt)
    return result.all()
