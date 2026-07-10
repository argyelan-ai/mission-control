"""
Agent-scoped endpoint: POST /api/v1/agent/x-posts

Creates a Draft -> Approve -> Post request for a single X (Twitter) post.
The actual tweepy call happens in the Approval hook (routers/approvals.py)
AFTER the operator approves — mirrors the install_requests.py pattern so
there is exactly one approval lifecycle in Mission Control, not a second one
for social posting.

Auth:   Agent PBKDF2 token
Scope:  content:submit
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.agent import Agent
from app.models.approval import Approval
from app.redis_client import RedisKeys, get_redis
from app.scopes import Scope, require_scope
from app.services.x_publisher import validate_draft

router = APIRouter(prefix="/api/v1/agent/x-posts", tags=["x-posts"])


# ── Schemas ───────────────────────────────────────────────────────────────


class XPostDraftCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=280)
    content_pipeline_id: uuid.UUID | None = None  # optional: write published_url back here
    task_id: uuid.UUID | None = None  # optional: callback comment on approval outcome


class XPostDraftResponse(BaseModel):
    approval_id: uuid.UUID
    status: str
    existing: bool
    warnings: list[str] = []


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.post("", response_model=XPostDraftResponse)
async def create_x_post_draft(
    body: XPostDraftCreate,
    response: Response,
    requester: Agent = Depends(require_scope(Scope.CONTENT_SUBMIT)),
    session: AsyncSession = Depends(get_session),
) -> XPostDraftResponse:
    """Create an x_post approval request for a single tweet draft.

    Returns 201 on new request, 200 if an identical pending request already exists.
    """
    validation = validate_draft(body.text)
    if not validation.ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(validation.errors))

    if requester.board_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Requester agent must be assigned to a board before x-post requests can be created",
        )

    # Idempotency: return existing pending request with the identical text
    existing_rows = (
        await session.exec(
            select(Approval).where(
                Approval.action_type == "x_post",
                Approval.status == "pending",
            )
        )
    ).all()
    for row in existing_rows:
        payload = row.payload or {}
        if payload.get("text") == body.text:
            response.status_code = status.HTTP_200_OK
            return XPostDraftResponse(
                approval_id=row.id,
                status=row.status,
                existing=True,
                warnings=validation.warnings,
            )

    description = f"{requester.name} requests to post to X: {body.text[:200]}"
    payload_data = {
        "text": body.text,
        "requester_agent_id": str(requester.id),
        "requester_agent_name": requester.name,
        "requester_task_id": str(body.task_id) if body.task_id else None,
        "content_pipeline_id": str(body.content_pipeline_id) if body.content_pipeline_id else None,
    }
    approval = Approval(
        board_id=requester.board_id,
        agent_id=requester.id,
        task_id=body.task_id,
        action_type="x_post",
        description=description,
        payload=payload_data,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        status="pending",
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    try:
        redis = await get_redis()
        await redis.publish(
            RedisKeys.approvals_events(),
            (
                f'{{"type":"approval.created","approval_id":"{approval.id}",'
                f'"action_type":"x_post"}}'
            ),
        )
    except Exception:
        pass  # SSE is best-effort; approval is already persisted

    response.status_code = status.HTTP_201_CREATED
    return XPostDraftResponse(
        approval_id=approval.id,
        status=approval.status,
        existing=False,
        warnings=validation.warnings,
    )
