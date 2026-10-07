"""/api/v1/browser-sessions — browser sessions of agents and head runs (ADR-088).

List and last image (viewer), open and end (operator). Only the open call returns the
session's gateway address (a repeat open of the same session returns the same,
deterministic address); the listing and the end call never contain it. Heads and agents get their sessions from the harness layer (ADR-088 harness-wiring step) through
`services/browser_sessions`, not through this API.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.models.agent import Agent
from app.models.browser_session import BROWSER_SESSION_STATUSES, BrowserSession
from app.services import browser_sessions as svc

router = APIRouter(prefix="/api/v1/browser-sessions", tags=["browser-sessions"])

_LIST_LIMIT = 200


class OpenBody(BaseModel):
    agent_id: Optional[uuid.UUID] = None
    head_run_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    @model_validator(mode="after")
    def _exactly_one_owner(self):
        if (self.agent_id is None) == (self.head_run_id is None):
            raise ValueError("give exactly one owner: agent_id or head_run_id")
        return self


class EndBody(BaseModel):
    reason: str = Field(default="operator", max_length=64)


def _public(row: BrowserSession) -> dict:
    """A session as the UI sees it — never the token or the address."""
    return {
        "id": str(row.id),
        "owner_kind": row.owner_kind,
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "head_run_id": row.head_run_id,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "end_reason": row.end_reason,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "last_active_at": row.last_active_at.isoformat() if row.last_active_at else None,
        "last_frame_at": row.last_frame_at.isoformat() if row.last_frame_at else None,
        "last_url": row.last_url,
        "last_title": row.last_title,
        "has_last_frame": svc.frame_path(row.id).is_file(),
    }


@router.get("", dependencies=[Depends(require_role(Role.VIEWER))])
async def list_browser_sessions(
    status: Optional[str] = Query(default=None, pattern="^(" + "|".join(BROWSER_SESSION_STATUSES) + ")$"),
    session: AsyncSession = Depends(get_session),
):
    query = select(BrowserSession).order_by(BrowserSession.created_at.desc()).limit(_LIST_LIMIT)
    if status:
        query = query.where(BrowserSession.status == status)
    return [_public(row) for row in (await session.exec(query)).all()]


@router.get("/{session_id}/last-frame", dependencies=[Depends(require_role(Role.VIEWER))])
async def browser_session_last_frame(session_id: uuid.UUID):
    """The session's last image (JPEG), kept after the browser part ended."""
    path = svc.frame_path(session_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no image for this browser session")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("", status_code=201, dependencies=[Depends(require_role(Role.OPERATOR))])
async def open_browser_session(body: OpenBody, session: AsyncSession = Depends(get_session)):
    agent = None
    if body.agent_id is not None:
        agent = await session.get(Agent, body.agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
    row, registered = await svc.open_session(session, agent=agent, head_run_id=body.head_run_id)
    return {**_public(row), "endpoint_path": svc.endpoint_path(row.id), "registered": registered}


@router.post("/{session_id}/end", dependencies=[Depends(require_role(Role.OPERATOR))])
async def end_browser_session(
    session_id: uuid.UUID, body: Optional[EndBody] = None, session: AsyncSession = Depends(get_session),
):
    row = await session.get(BrowserSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="browser session not found")
    result = await svc.end_session(session, row, reason=(body or EndBody()).reason)
    return {**_public(row), "gateway": result}
