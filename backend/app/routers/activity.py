import uuid

from fastapi import APIRouter, Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.auth import require_user
from app.database import get_session
from app.models.activity import ActivityEvent
from app.redis_client import RedisKeys
from app.services.sse import make_sse_response

router = APIRouter(prefix="/api/v1", tags=["activity"])


@router.get("/activity")
async def list_activity(
    board_id: uuid.UUID | None = Query(None),
    agent_id: uuid.UUID | None = Query(None),
    event_type: str | None = Query(None),
    severity: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_user),
):
    query = select(ActivityEvent)
    if board_id:
        query = query.where(ActivityEvent.board_id == board_id)
    if agent_id:
        query = query.where(ActivityEvent.agent_id == agent_id)
    if event_type:
        query = query.where(ActivityEvent.event_type == event_type)
    if severity:
        query = query.where(ActivityEvent.severity == severity)
    query = query.order_by(ActivityEvent.created_at.desc()).offset(offset).limit(limit)  # type: ignore[attr-defined]
    result = await session.exec(query)
    return result.all()


@router.get("/activity/stream")
async def stream_activity(current_user = Depends(require_user)):
    return make_sse_response([RedisKeys.activity_events()])
