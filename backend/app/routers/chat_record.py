"""Endpoints of MC's chat record (ADR-103) and its chats (S2).

Reads: signed-in users (role viewer or higher). Writes (S2 — a further chat
on an agent's harness, rename/archive, pin the native session, the resume
plan): admins, the same role the agent chat's input routes need. The
harness side (omp bridge after ACP ``session/new``, the Claude start
script) holds an agent token, not a user JWT: ``/agent/me/chats/…`` are the
pin and resume twins for it, scope ``chat:write`` for the pin, and only the
calling agent's own chats exist there. The privacy gate of the
transcript adapters (``transcript_allowed``) runs when an event is WRITTEN
(importer, live sink); what is in the record stays readable here. While ``CHAT_RECORD_ENABLED`` is off every route
answers 404 ``{"reason": "chat_record_disabled"}`` — the record does not exist
for the product until the flag is on.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_agent, require_role
from app.database import get_session
from app.scopes import Scope, require_scope
from app.services import chat_record, chat_switch

router = APIRouter(prefix="/api/v1/chat-record", tags=["chat-record"])

#: An event kind as the record stores it (``chat_events.kind``, 40 chars).
_KIND_RE = re.compile(r"[a-z][a-z_]{0,39}")
_MAX_KINDS = 10


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
    kind: list[str] | None = Query(None),
    current_user=Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
):
    """One page of a chat's record across all its sessions, oldest first;
    ``before`` = an event id for backward paging; ``kind`` (repeatable) =
    only events of these kinds, e.g. ``kind=switch`` for every switch card
    of the chat, however old."""
    _require_enabled()
    kinds = kind or []
    if len(kinds) > _MAX_KINDS or any(not _KIND_RE.fullmatch(k) for k in kinds):
        raise HTTPException(status_code=422, detail={"reason": "bad_kind"})
    chat = await chat_record.get_chat(session, current_user, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history = await chat_record.chat_history(session, chat.id, limit=limit, before=before, kinds=kinds)
    history["chat"] = {
        "id": str(chat.id),
        "title": chat.title,
        "harness": chat.harness,
        "model": chat.model,
        "turnState": chat.turn_state,
        "turnStateAt": chat.turn_state_at.isoformat() if chat.turn_state_at else None,
    }
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


class SwitchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    harness: str = Field(max_length=40)
    runtime_id: uuid.UUID | None = Field(default=None, alias="runtimeId")
    #: The service's rule (no whitespace or control characters); empty = keep.
    model: str | None = Field(default=None, max_length=200, pattern="^$|" + chat_switch.MODEL_PATTERN)
    #: Only needed while a turn runs (409 ``turn_running`` lists the options).
    mode: str | None = Field(default=None, max_length=40)


class SwitchBackBody(BaseModel):
    mode: str | None = Field(default=None, max_length=40)


class SwitchErrorBody(BaseModel):
    code: str = Field(max_length=40)
    detail: str | None = Field(default=None, max_length=2000)


class SwitchResultBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(pattern=r"^(done|failed)$")
    native_session_id: str | None = Field(
        default=None, alias="nativeSessionId", pattern=chat_record.NATIVE_ID_PATTERN
    )
    #: The harness resumed the session natively (omp ``session/load``) and
    #: did not need the package.
    native_resume: bool = Field(default=False, alias="nativeResume")
    real_tokens: int | None = Field(default=None, alias="realTokens", ge=0)
    error: SwitchErrorBody | None = None


def _raise_switch(exc: chat_switch.SwitchError) -> None:
    raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc


@router.post("/chats/{chat_id}/switch")
async def switch_record_chat(
    chat_id: uuid.UUID,
    body: SwitchBody,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Moves the chat to another pair (ADR-105). Model only: the running
    session changes model, the pair is written when the next answer comes
    from it. Other runtime or harness: the switch row is written first, then
    the harness side starts the new session with the handoff package and
    reports back. Refusals: 400 ``pair_incompatible`` / ``no_change``, 409
    ``turn_running`` (with ``options``) / ``switch_pending`` /
    ``default_chat_follows_agent``, 423 ``box_locked``."""
    _require_enabled()
    chat = await _visible_chat(session, current_user, chat_id)
    try:
        return await chat_switch.request_switch(
            session, current_user, chat,
            harness=body.harness, runtime_id=body.runtime_id, model=body.model, mode=body.mode,
        )
    except chat_switch.SwitchError as exc:
        _raise_switch(exc)


@router.post("/chats/{chat_id}/switch-back")
async def switch_back_record_chat(
    chat_id: uuid.UUID,
    body: SwitchBackBody | None = None,
    current_user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """Back to the pair before the last switch, resuming that session."""
    _require_enabled()
    chat = await _visible_chat(session, current_user, chat_id)
    try:
        return await chat_switch.switch_back(session, current_user, chat, mode=body.mode if body else None)
    except chat_switch.SwitchError as exc:
        _raise_switch(exc)


@router.get("/chats/{chat_id}/switch/{event_id}")
async def get_record_chat_switch(
    chat_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user=Depends(require_role(Role.VIEWER)),
    session: AsyncSession = Depends(get_session),
):
    """One switch with its state and package text (the switch card)."""
    _require_enabled()
    chat = await _visible_chat(session, current_user, chat_id)
    try:
        return await chat_switch.get_switch(session, chat, event_id)
    except chat_switch.SwitchError as exc:
        _raise_switch(exc)


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


# ── Harness side (agent token) ───────────────────────────────────────────


async def _own_chat(session: AsyncSession, agent, chat_id: uuid.UUID):
    """A chat on the calling agent's harness — any other chat is 404, so an
    agent cannot probe or move another agent's record."""
    from app.models.chat_record import Chat

    chat = await session.get(Chat, chat_id)
    if chat is None or chat.legacy_agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.put("/agent/me/chats/{chat_id}/native-session")
async def agent_pin_chat_session(
    chat_id: uuid.UUID,
    body: PinBody,
    agent=Depends(require_scope(Scope.CHAT_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """The harness reports the native session it opened for this chat (omp:
    the id ACP ``session/new`` returned). Same rules as the admin route."""
    _require_enabled()
    chat = await _own_chat(session, agent, chat_id)
    try:
        moved = await chat_record.pin_native_session(session, chat, body.native_session_id)
    except chat_record.ChatRecordError as exc:
        _raise(exc)
    await session.commit()
    return {**chat_record.chat_out(chat), "movedEvents": moved}


@router.get("/agent/me/chats/{chat_id}/resume")
async def agent_chat_resume(
    chat_id: uuid.UUID,
    agent=Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    """How the harness puts this chat's own session in front of the model
    (same plan as the admin route). Data only; starts nothing."""
    _require_enabled()
    chat = await _own_chat(session, agent, chat_id)
    return await chat_record.resume_plan(session, chat)


@router.put("/agent/me/chats/{chat_id}/switch/{event_id}/result")
async def agent_report_switch_result(
    chat_id: uuid.UUID,
    event_id: uuid.UUID,
    body: SwitchResultBody,
    agent=Depends(require_scope(Scope.CHAT_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """The harness side reports how a restart ended: ``done`` once the new
    session answered (with its native id), or ``failed`` with a code. Only
    ``done`` changes the chat's pair and session."""
    _require_enabled()
    chat = await _own_chat(session, agent, chat_id)
    try:
        return await chat_switch.report_result(
            session, chat, event_id,
            status=body.status,
            native_session_id=body.native_session_id,
            native_resume=body.native_resume,
            error=body.error.model_dump() if body.error else None,
            real_tokens=body.real_tokens,
        )
    except chat_switch.SwitchError as exc:
        _raise_switch(exc)
