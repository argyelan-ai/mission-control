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

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
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
    AgentStartingError,
    EffortSwitchFailedError,
    EffortSwitchRejectedError,
    InputNotSupportedError,
    can_receive_input,
    effort_capabilities,
    model_options_capabilities,
    send_keys,
    send_text,
    set_effort,
    slash_command_capabilities,
)
from app.services.harness_catalog import get_observed_model_windows
from app.services.reference_ingest import (
    ReferenceIngestError,
    ReferenceTooLargeError,
    is_image_reference,
    serialize_reference,
    store_reference,
)
from app.services.sse import _sse_generator
from app.services.transcript_chat import (
    find_active_session,
    read_history,
    resolve_aliveness,
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
_AGENT_STARTING = {"reason": "agent_starting"}
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
    (``{"effortLevels": [...], "canSwitchEffort": bool, "slashCommands":
    [...], "modelOptions": [...]}``) so the composer can build its effort
    chip, command palette, and model dropdown from what this agent's
    harness actually supports, instead of a hardcoded frontend list — see
    ``agent_chat_input.effort_capabilities`` / ``slash_command_capabilities``
    / ``model_options_capabilities`` for each derivation (the latter two,
    and each ``usage`` event's ``contextWindow`` estimate, use the
    harness-catalog Redis-backed discovery + observed-window map, fetched
    once here and threaded into ``read_history`` — see
    ``harness_catalog``'s module docstring). Also stamps
    ``session.aliveness`` (``"active" | "idle" | "ended"`` —
    ``transcript_chat.resolve_aliveness``): the old ``session.live`` alone
    (mtime<60s) read an idle-but-still-running CLI as "ended" everywhere,
    an operator-visible bug; ``live`` is kept unchanged for backward
    compat (== ``aliveness == "active"``)."""
    resolved = await _resolve_transcript_path(agent_id, session)
    if isinstance(resolved, JSONResponse):
        return resolved

    agent, path = resolved
    observed_windows = await get_observed_model_windows()
    history = read_history(
        path, limit=limit, before_uuid=before_uuid, observed_windows=observed_windows
    )
    history["session"]["aliveness"] = await resolve_aliveness(agent, path)
    history["capabilities"] = {
        **await effort_capabilities(agent),
        **await slash_command_capabilities(agent),
        **await model_options_capabilities(agent),
    }
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
    Boss (mirrors A2's runtime gating); 409 ``{"reason":"agent_starting"}``
    (docker only) when the pane never became ready within ``send_text``'s
    readiness gate — the CLI is still booting/loading plugins or a recycler
    respawn is mid-flight, and nothing was typed (see
    ``agent_chat_input._wait_for_send_readiness``)."""
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
    except AgentStartingError:
        return JSONResponse(status_code=409, content=_AGENT_STARTING)


@router.post("/agents/{agent_id}/chat/attachment", status_code=201)
async def post_chat_attachment(
    agent_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Nimmt eine Datei entgegen und gibt den absoluten Pfad zurueck, unter
    dem der Agent sie lesen kann.

    Abgelegt wird sie als Agenten-Referenz — dieselbe Ablage, die der
    Slack-Datei-Ingest schon benutzt (``reference_files.agent_id``, Migration
    0172). Das ist keine Bequemlichkeit, sondern der Grund, aus dem es die
    Besitz-Art ueberhaupt gibt: eine Datei, die der Operator top-level im
    Chat schickt, gehoert dem AGENTEN und keiner Aufgabe. Sie wird damit
    automatisch mit ihm geloescht (``delete_references_for(agent_id=…)`` in
    routers/agents.py) statt verwaist liegen zu bleiben.

    Der Composer haengt den Pfad danach an die Nachricht — die CLI liest die
    Datei selbst. Es gibt bewusst KEINE Typen-Beschraenkung und keinen
    20er-Deckel (Operator-Entscheid 19.08.2026): ob ein Agent eine Datei
    versteht, ist seine Sache, das UI legt nur ab. Gefaehrlich ist das nicht
    — aktive Inhalte liefert ``fs_service.read_stream`` grundsaetzlich als
    Download aus, nie inline.

    409 ``{"reason":"input_not_supported"}`` fuer Agenten, die ueberhaupt
    keinen Chat-Text annehmen (Host-Agenten ausser Boss): dort waere die
    Datei nur Platte ohne Empfaenger. 413 wenn zu gross, 422 bei einem
    unbrauchbaren oder leeren Upload."""
    agent = await _load_agent_or_404(agent_id, session)

    if not can_receive_input(agent):
        return JSONResponse(status_code=409, content=_INPUT_NOT_SUPPORTED)

    contents = await file.read()
    if not contents:
        # Frueh und eigenstaendig: eine leere Datei ist kein Ingest-Problem,
        # sondern eine Auswahl, die niemandem nuetzt — der Agent bekaeme
        # einen Pfad auf 0 Bytes.
        raise HTTPException(status_code=422, detail="Die Datei ist leer.")

    try:
        ref = await store_reference(
            session,
            contents=contents,
            filename=file.filename or "",
            mime=file.content_type,
            agent_id=agent.id,
            uploaded_by="chat",
            # Die zwei Huerden, die fuer einen laufenden Chat nicht passen.
            allowed_mimes=None,
            max_files=None,
        )
    except ReferenceTooLargeError as exc:
        # Zu gross ist die einzige Ablehnung, die der Nutzer beim Auswaehlen
        # nicht sehen konnte — sie bekommt darum ihren eigenen Status, damit
        # das UI sie als Hinweis statt als Fehler zeigen kann. Eigene
        # Fehlerklasse statt Textsuche: ein Umformulieren der Meldung darf
        # den Status nie kippen.
        raise HTTPException(status_code=413, detail=str(exc))
    except ReferenceIngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "path": serialize_reference(ref)["abs_path"],
        "name": ref.original_name,
        "bytes": ref.size,
        "isImage": is_image_reference(ref.original_name),
        # Root + Unterpfad direkt aus der Ablage: das Frontend holt die Bytes
        # ueber den Files-Endpunkt und muss sie sonst aus dem absoluten Pfad
        # zurueckrechnen (siehe ChatAttachmentTile.toFilesRef).
        "root": "references",
        "subpath": ref.rel_path,
    }


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
    ``{"reason":"effort_switch_rejected","message":str}`` when the CLI
    EXPLICITLY declined the switch (its own ``"Kept effort level as <X>"``
    wording, live-verified on Davinci — see
    ``agent_chat_input.EffortSwitchRejectedError``'s docstring) — the CLI's
    own message is included so the UI can show the operator WHY, distinct
    from 409 ``{"reason":"effort_switch_failed"}`` when verification simply
    timed out with no explicit answer either way (see
    ``agent_chat_input.set_effort``).

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
    except EffortSwitchRejectedError as e:
        return JSONResponse(
            status_code=409,
            content={"reason": "effort_switch_rejected", "message": e.cli_message},
        )
    except EffortSwitchFailedError:
        return JSONResponse(status_code=409, content=_EFFORT_SWITCH_FAILED)
