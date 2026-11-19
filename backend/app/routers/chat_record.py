"""Read endpoints of MC's chat record (ADR-103).

Read-only, signed-in users (role viewer or higher). The privacy gate of the
transcript adapters (``transcript_allowed``) runs when an event is WRITTEN
(importer, live sink); what is in the record stays readable here. While ``CHAT_RECORD_ENABLED`` is off every route
answers 404 ``{"reason": "chat_record_disabled"}`` — the record does not exist
for the product until the flag is on.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.services import chat_record

router = APIRouter(prefix="/api/v1/chat-record", tags=["chat-record"])


def _require_enabled() -> None:
    if not chat_record.enabled():
        raise HTTPException(status_code=404, detail={"reason": "chat_record_disabled"})


@router.get("/chats")
async def list_record_chats(
    limit: int = Query(100, ge=1, le=500),
    current_user=Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
):
    """The chat list from the record, newest activity first."""
    _require_enabled()
    return {"chats": await chat_record.list_chats(session, current_user, limit=limit)}


@router.get("/search")
async def search_record(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    current_user=Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
):
    """Search across every chat the user can see (Postgres full-text)."""
    _require_enabled()
    return {"hits": await chat_record.search(session, current_user, q, limit=limit)}


@router.get("/chats/{chat_id}/history")
async def get_record_history(
    chat_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=1000),
    before: uuid.UUID | None = Query(None),
    current_user=Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
):
    """One page of a chat's record across all its sessions, oldest first;
    ``before`` = an event id for backward paging."""
    _require_enabled()
    chat = await chat_record.get_chat(session, current_user, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history = await chat_record.chat_history(session, chat.id, limit=limit, before=before)
    history["chat"] = {"id": str(chat.id), "title": chat.title, "harness": chat.harness, "model": chat.model}
    return history
