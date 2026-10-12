"""Live voice in the chat (ADR-091 draft, voice PR 1).

- ``GET  /api/v1/voice/live/config``  — is the feature on? 404 when off or
  no OpenAI key, so the UI shows no mic at all (DESIGN K3: nothing empty).
- ``POST /api/v1/voice/live/session`` — mint a short-lived client secret for
  one call with the given chat agent. The browser uses it once to open WebRTC
  to OpenAI; the long-lived key stays here.

Admin-only, like typing into the chat (``agent_chat.py`` chat/input): the
voice talks on the operator's behalf. Separate from ``routers/voice.py``
(Jarvis / LiveKit), which this does not touch.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.models.agent import Agent
from app.config import settings
from app.services import voice_live

router = APIRouter(prefix="/api/v1/voice/live", tags=["voice-live"])


class VoiceLiveConfig(BaseModel):
    enabled: bool
    model: str
    voice: str


class VoiceLiveSessionBody(BaseModel):
    agent_id: uuid.UUID
    lang: Literal["de", "en"] = "en"


class VoiceLiveSession(BaseModel):
    client_secret: str
    expires_at: int
    model: str
    voice: str
    calls_url: str
    agent_name: str


def _require_enabled() -> None:
    if not voice_live.is_enabled():
        raise HTTPException(status_code=404, detail="voice_live_disabled")


@router.get("/config", response_model=VoiceLiveConfig)
async def get_config(_user=Depends(require_role(Role.ADMIN))) -> VoiceLiveConfig:
    _require_enabled()
    return VoiceLiveConfig(
        enabled=True, model=settings.voice_live_model, voice=settings.voice_live_voice
    )


@router.post("/session", response_model=VoiceLiveSession)
async def create_session(
    body: VoiceLiveSessionBody,
    _user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    _require_enabled()
    agent = await session.get(Agent, body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        minted = await voice_live.mint_client_secret(agent.name, body.lang)
    except voice_live.VoiceLiveError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": {"reason": exc.reason}})
    return VoiceLiveSession(
        client_secret=minted.client_secret,
        expires_at=minted.expires_at,
        model=minted.model,
        voice=minted.voice,
        calls_url=minted.calls_url,
        agent_name=agent.name,
    )
