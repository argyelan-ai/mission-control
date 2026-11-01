"""Talking across harnesses — agent-side routes (ADR-100).

Prefix `/api/v1/agent/comms`. Every route needs a comms principal
(`app.comms_auth.require_comms_principal`); "alive" and "listening" come
only from `services.comms_directory` (§6 rule 2).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.comms_auth import require_comms_principal
from app.database import get_session
from app.services import comms_directory, comms_send
from app.services.comms_directory import Principal

router = APIRouter(prefix="/api/v1/agent/comms", tags=["agent-comms"])


class PresenceBody(BaseModel):
    listening: bool = True
    busy: bool = False
    task: str | None = Field(default=None, max_length=200)
    accept: Literal["accept", "hold", "refuse"] = "accept"


@router.post("/presence")
async def post_presence(
    body: PresenceBody,
    principal: Principal = Depends(require_comms_principal),
):
    """Sign of life plus "listening / busy / current task / accept mode".
    Expires after 90 s; send it again to stay alive."""
    await comms_directory.record_presence(
        principal, listening=body.listening, busy=body.busy, task=body.task, accept=body.accept,
    )
    return {"ok": True, "address": principal.address, "expires_in": comms_directory.PRESENCE_TTL_S}


@router.get("/who")
async def who(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_comms_principal),
):
    """The directory: every principal that may talk, with "alive"
    (yes / no / unknown) and "listening" kept apart."""
    return {"me": principal.address, **(await comms_directory.directory(session))}


class SendBody(BaseModel):
    # extra="forbid": the sender comes from the token; a body that tries to
    # name one ("from", "sender") is refused, not silently ignored.
    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=comms_send.MAX_BODY)


@router.post("/send", status_code=status.HTTP_201_CREATED)
async def send(
    payload: SendBody,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_comms_principal),
):
    """Send a message to one named session (ADR-100 §6). Information only —
    a message never grants a right. Refusals carry a `code`: no_broadcast,
    self, not_found, ambiguous (+ candidates), rate_limited, not_listening,
    refused, duplicate, inbox_full, paused_awaiting_operator,
    stopped_by_operator."""
    try:
        return await comms_send.send(session, principal, payload.to, payload.body)
    except comms_send.SendRefused as refused:
        raise HTTPException(status_code=refused.status, detail=refused.detail())
