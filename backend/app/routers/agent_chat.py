"""Live chat view over an agent's Claude Code transcript — HTTP history page
plus a live SSE tail. Parsing/session-resolution lives in
``services/transcript_chat.py`` (A1-A3); this router only wires auth, the
404 gating contract, and the tailer's acquire/release lifecycle around it.
"""
from __future__ import annotations

import asyncio
import re
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
from app.services.agent_chat_input import (
    AgentBusyError,
    EffortSwitchFailedError,
    InputNotSupportedError,
    effort_capabilities,
    send_keys,
    send_text,
    set_effort,
)
from app.services.sse import _sse_generator
from app.services.transcript_chat import (
    find_active_session,
    read_history,
    resolve_transcript_dir,
    tailer_manager,
    transcript_allowed,
)
from app.services.workspace_diff import NoWorkspaceError, resolve_workspace_path, workspace_diff

router = APIRouter(prefix="/api/v1", tags=["agent-chat"])

_NO_TRANSCRIPT = {"reason": "no_transcript"}
_NO_WORKSPACE = {"reason": "no_workspace"}
_INPUT_NOT_SUPPORTED = {"reason": "input_not_supported"}
_EFFORT_SWITCH_FAILED = {"reason": "effort_switch_failed"}
_AGENT_BUSY = {"reason": "agent_busy"}
_MAX_TEXT_LEN = 20000
_MAX_KEYS_LEN = 16

# C0 control chars other than \t (0x09) and \n (0x0a) — NUL in particular
# makes ``subprocess.run`` raise ValueError deep inside delivery, which would
# otherwise surface as an unhandled 500 instead of a clean 422 (fix round 1).
_DISALLOWED_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f]")


class ChatInputBody(BaseModel):
    text: str


class ChatKeysBody(BaseModel):
    keys: list[str]


class ChatEffortBody(BaseModel):
    level: str


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
    """History page plus a ``capabilities`` block
    (``{"effortLevels": [...], "canSwitchEffort": bool}``) so the composer's
    effort chip can build itself from what this agent's harness actually
    supports instead of a hardcoded level list — see
    ``agent_chat_input.effort_capabilities`` for the derivation (docker/
    cli-bridge gets the discovered level list, every other runtime gets an
    empty list and ``canSwitchEffort=False``)."""
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return resolved

    agent, path = resolved
    history = read_history(path, limit=limit, before_uuid=before_uuid)
    history["capabilities"] = effort_capabilities(agent)
    return history


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

    agent, path = resolved
    channel = RedisKeys.agent_chat_channel(str(agent_id))

    async def _generator():
        await tailer_manager.acquire(str(agent_id), path, agent)
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


@router.get("/agents/{agent_id}/chat/diff")
async def get_chat_diff(
    agent_id: uuid.UUID,
    scope: str = Query("worktree", pattern="^(worktree|last-commit)$"),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Structured git diff over the agent's workspace: uncommitted changes
    (``scope=worktree``, default) or the most recent commit
    (``scope=last-commit``). 404 ``{"reason": "no_workspace"}`` when the
    agent has no ``workspace_path``, the path doesn't exist on disk, isn't a
    git repository, or (``last-commit`` only) has no commits yet."""
    agent = await _load_agent_or_404(agent_id, session)

    if not agent.workspace_path:
        return JSONResponse(status_code=404, content=_NO_WORKSPACE)

    workspace = resolve_workspace_path(agent.workspace_path)
    try:
        diff = await asyncio.to_thread(workspace_diff, workspace, scope)
    except NoWorkspaceError:
        return JSONResponse(status_code=404, content=_NO_WORKSPACE)

    return diff


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
    if _DISALLOWED_CONTROL_CHARS.search(body.text):
        raise HTTPException(
            status_code=422, detail="text contains disallowed control characters"
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

    if len(body.keys) > _MAX_KEYS_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"too many keys (max {_MAX_KEYS_LEN} per request)",
        )

    try:
        await send_keys(agent, body.keys)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)


@router.post("/agents/{agent_id}/chat/effort", status_code=204)
async def post_chat_effort(
    agent_id: uuid.UUID,
    body: ChatEffortBody,
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Switches the agent's effort level via ``/effort <level>`` (v1:
    cli-bridge/docker agents only — Boss and every other host agent get 409
    ``{"reason":"input_not_supported"}``, no pane probe exists for them).
    422 on a non-allowlisted level; 409 ``{"reason":"agent_busy"}`` when the
    pane shows a working turn or an open permission prompt (refused before
    touching the TUI at all — Escape is this app's INTERRUPT key, not a
    neutral cleanup, wave-review I-1); 409
    ``{"reason":"effort_switch_failed"}`` when the switch couldn't be
    verified as applied (see ``agent_chat_input.set_effort``).

    NOTE (Phase-0 discovery, empirically verified): this also changes the
    agent's PERSISTED default effort level in its ``settings.json`` — Claude
    Code 2.1.233 has no way to change effort session-only, not even via the
    ``/model`` picker's "s" option (which does correctly scope a MODEL
    choice to the session, just not effort). Every chat-triggered effort
    switch is a durable change to what a fresh session for this agent starts
    at, until switched again."""
    agent = await _load_agent_or_404(agent_id, session)

    try:
        await set_effort(agent, body.level)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except InputNotSupportedError:
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)
    except AgentBusyError:
        return JSONResponse(status_code=409, content=_AGENT_BUSY)
    except EffortSwitchFailedError:
        return JSONResponse(status_code=409, content=_EFFORT_SWITCH_FAILED)
