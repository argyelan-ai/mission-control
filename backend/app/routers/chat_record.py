"""Endpoints of MC's chat record (ADR-103) and its chats (S2).

Reads: signed-in users (role viewer or higher). Writes (S2 — a further chat
on an agent's harness, rename/archive, pin the native session, the resume
plan): admins, the same role the agent chat's input routes need. The privacy gate of the
transcript adapters (``transcript_allowed``) runs when an event is WRITTEN
(importer, live sink); what is in the record stays readable here. While ``CHAT_RECORD_ENABLED`` is off every route
answers 404 ``{"reason": "chat_record_disabled"}`` — the record does not exist
for the product until the flag is on.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.services import chat_record

router = APIRouter(prefix="/api/v1/chat-record", tags=["chat-record"])


def _require_enabled() -> None:
    if not chat_record.enabled():
        raise HTTPException(status_code=404, detail={"reason": "chat_record_disabled"})


def _raise(exc: chat_record.ChatRecordError) -> None:
    raise HTTPException(status_code=exc.status, detail={"reason": exc.reason}) from exc


class CreateChatBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent_id: uuid.UUID = Field(alias="agentId")
    title: str | None = Field(default=None, max_length=200)


class UpdateChatBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None
    pinned: bool | None = None


class PinBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    native_session_id: str = Field(alias="nativeSessionId", pattern=chat_record.NATIVE_ID_PATTERN)


@router.get("/chats")
async def list_record_chats(
    limit: int = Query(100, ge=1, le=500),
    agent_id: uuid.UUID | None = Query(None, alias="agentId"),
    current_user=Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
):
    """The chat list from the record, newest activity first; ``agentId``
    = only the chats on that agent's harness."""
    _require_enabled()
    return {"chats": await chat_record.list_chats(session, current_user, limit=limit, agent_id=agent_id)}


@router.post("/chats", status_code=201)
async def create_record_chat(
    body: CreateChatBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """A further chat on the harness of an existing agent (S2). It gets its
    own native session; ``GET …/resume`` says how the harness starts it."""
    _require_enabled()
    from app.models.agent import Agent

    agent = await session.get(Agent, body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail={"reason": "agent_not_found"})
    try:
        chat = await chat_record.create_chat(session, current_user, agent, title=body.title)
    except chat_record.ChatRecordError as exc:
        _raise(exc)
    await session.commit()
    return chat_record.chat_out(chat)


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


async def _visible_chat(session: AsyncSession, user, chat_id: uuid.UUID):
    chat = await chat_record.get_chat(session, user, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.patch("/chats/{chat_id}")
async def update_record_chat(
    chat_id: uuid.UUID,
    body: UpdateChatBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Rename, archive / unarchive or pin a chat. An agent's default chat
    cannot be archived (409 ``default_chat``)."""
    _require_enabled()
    chat = await _visible_chat(session, current_user, chat_id)
    try:
        await chat_record.update_chat(
            session, chat, title=body.title, archived=body.archived, pinned=body.pinned
        )
    except chat_record.ChatRecordError as exc:
        _raise(exc)
    await session.commit()
    return chat_record.chat_out(chat)


@router.put("/chats/{chat_id}/native-session")
async def pin_record_chat_session(
    chat_id: uuid.UUID,
    body: PinBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Binds the chat to its native session (omp: the id ACP ``session/new``
    returned). Same id again = no-op; another id = 409 ``already_pinned``;
    an id another chat of the agent holds = 409 ``pinned_by_other_chat``."""
    _require_enabled()
    chat = await _visible_chat(session, current_user, chat_id)
    try:
        moved = await chat_record.pin_native_session(session, chat, body.native_session_id)
    except chat_record.ChatRecordError as exc:
        _raise(exc)
    await session.commit()
    return {**chat_record.chat_out(chat), "movedEvents": moved}


@router.get("/chats/{chat_id}/resume")
async def get_record_chat_resume(
    chat_id: uuid.UUID,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """How the harness puts this chat's own session in front of the model:
    ``start`` (Claude ``--session-id``, ACP ``session/new``) or ``resume``
    (Claude ``--resume``, ACP ``session/load``). Data only; starts nothing."""
    _require_enabled()
    chat = await _visible_chat(session, current_user, chat_id)
    return await chat_record.resume_plan(session, chat)
