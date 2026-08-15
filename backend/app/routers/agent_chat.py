"""Live chat view over an agent's Claude Code transcript — HTTP history page
plus a live SSE tail. Parsing/session-resolution lives in
``services/transcript_chat.py`` (A1-A3); this router only wires auth, the
404 gating contract, and the tailer's acquire/release lifecycle around it.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.auth import require_user
from app.database import get_session
from app.models.agent import Agent
from app.redis_client import RedisKeys
from app.services.agent_chat_input import InputNotSupportedError, send_keys, send_text
from app.services.sse import _sse_generator
from app.services.transcript_chat import (
    find_active_session,
    read_history,
    resolve_transcript_dir,
    tailer_manager,
    transcript_allowed,
)

router = APIRouter(prefix="/api/v1", tags=["agent-chat"])

_NO_TRANSCRIPT = {"reason": "no_transcript"}
_INPUT_NOT_SUPPORTED = {"reason": "input_not_supported"}
_MAX_TEXT_LEN = 20000


class ChatInputBody(BaseModel):
    text: str


class ChatKeysBody(BaseModel):
    keys: list[str]


async def _resolve_transcript_path(
    agent_id: uuid.UUID, session: AsyncSession
) -> tuple[Agent, Path] | JSONResponse:
    """Loads the agent and its live session's transcript path, or the exact
    404 body the frontend keys on (``{"reason": "no_transcript"}``) for
    every "nothing to show" case: no transcript dir for this agent/runtime,
    no ``.jsonl`` session in that dir yet, or the Boss privacy gate
    rejecting the newest session's cwd. A genuinely unknown ``agent_id``
    raises a plain 404 instead — that's a routing error, not a "no session
    yet" state the frontend renders specially.
    """
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    tdir = resolve_transcript_dir(agent)
    if tdir is None:
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    active = find_active_session(tdir)
    if active is None:
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    path, _meta = active
    if not transcript_allowed(agent, path):
        return JSONResponse(status_code=404, content=_NO_TRANSCRIPT)

    return agent, path


@router.get("/agents/{agent_id}/chat/history")
async def get_chat_history(
    agent_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=1000),
    before_uuid: str | None = Query(None),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return resolved

    _agent, path = resolved
    return read_history(path, limit=limit, before_uuid=before_uuid)


@router.get("/agents/{agent_id}/chat/stream")
async def stream_agent_chat(
    agent_id: uuid.UUID,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """SSE tail of the agent's live transcript. Wraps the shared
    ``_sse_generator`` — acquires the tailer (starting its poll task on the
    first connected client for this agent) before the first frame, and
    releases it in ``finally`` once the client disconnects (cancelling the
    poll task if this was the last client)."""
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return resolved

    _agent, path = resolved
    channel = RedisKeys.agent_chat_channel(str(agent_id))

    async def _generator():
        await tailer_manager.acquire(str(agent_id), path)
        try:
            async for frame in _sse_generator([channel]):
                yield frame
        finally:
            await tailer_manager.release(str(agent_id))

    return EventSourceResponse(_generator())


async def _load_agent_or_404(agent_id: uuid.UUID, session: AsyncSession) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("/agents/{agent_id}/chat/input", status_code=204)
async def post_chat_input(
    agent_id: uuid.UUID,
    body: ChatInputBody,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Types ``body.text`` into the agent's live session (tmux send-keys for
    cli-bridge agents, host-pty-bridge WS for Boss). 422 for empty/oversized
    text; 409 ``{"reason":"input_not_supported"}`` for host agents other than
    Boss (mirrors A2's runtime gating)."""
    agent = await _load_agent_or_404(agent_id, session)

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")
    if len(body.text) > _MAX_TEXT_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"text too long (max {_MAX_TEXT_LEN} chars)",
        )

    try:
        await send_text(agent, body.text)
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)


@router.post("/agents/{agent_id}/chat/keys", status_code=204)
async def post_chat_keys(
    agent_id: uuid.UUID,
    body: ChatKeysBody,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Sends a sequence of allowlisted control keys (Escape/Enter/Up/Down/
    digits/y/n) to the agent's live session. 422 on any non-allowlisted key;
    409 ``{"reason":"input_not_supported"}`` for host agents other than
    Boss."""
    agent = await _load_agent_or_404(agent_id, session)

    try:
        await send_keys(agent, body.keys)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
