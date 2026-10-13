"""Live voice in the chat (ADR-091 draft, voice PR 1).

- ``GET  /api/v1/voice/live/config``  — is the feature on? 404 when off or
  no OpenAI key, so the UI shows no mic at all (DESIGN K3: nothing empty).
- ``GET  /api/v1/voice/live/default-target`` — which agent the navigation
  mic talks to: the lead agent of the active board (voice PR 5). 404
  ``no_lead`` when the fleet has none — the UI then keeps Jarvis on that mic.
- ``POST /api/v1/voice/live/session`` — mint a short-lived client secret for
  one call with the given chat agent. The browser uses it once to open WebRTC
  to OpenAI; the long-lived key stays here. Also returns ``call_ref``: the
  call's handle for the tool endpoint below (voice PR 2).
- ``POST /api/v1/voice/live/tool`` — runs one tool call the realtime model
  emitted (``send_to_agent`` / ``get_agent_status`` / ``stop_agent``) for
  the agent the call was opened for. See ``services/voice_live_tools.py``.
- ``POST /api/v1/voice/live/progress`` — one redacted context line for the
  voice about the agent's progress (voice PR 3). The browser watches the
  chat's live stream and says WHEN; ``services/voice_progress.py`` says WHAT.

Admin-only, like typing into the chat (``agent_chat.py`` chat/input): the
voice talks on the operator's behalf. Separate from ``routers/voice.py``
(Jarvis / LiveKit), which this does not touch.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.database import get_session
from app.models.agent import Agent
from app.config import settings
from app.services import voice_live, voice_live_tools, voice_progress
from app.services.voice_live_tools import VoiceToolError

logger = logging.getLogger("mc.voice_live")

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
    call_ref: str


def _require_enabled() -> None:
    if not voice_live.is_enabled():
        raise HTTPException(status_code=404, detail="voice_live_disabled")


@router.get("/config", response_model=VoiceLiveConfig)
async def get_config(_user=Depends(require_role(Role.ADMIN))) -> VoiceLiveConfig:
    _require_enabled()
    return VoiceLiveConfig(
        enabled=True, model=settings.voice_live_model, voice=settings.voice_live_voice
    )


class VoiceLiveTarget(BaseModel):
    agent_id: uuid.UUID
    agent_name: str


@router.get("/default-target", response_model=VoiceLiveTarget)
async def get_default_target(
    board_id: uuid.UUID | None = None,
    _user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> VoiceLiveTarget:
    """The lead agent the navigation mic calls (operator rule 12.10.2026).

    The active board's lead first; with no board picked yet or a board
    without a lead, the fleet's lead (oldest first, so the answer is stable).
    The name comes from the data — the public repo never hardcodes it.
    """
    _require_enabled()
    base = (
        select(Agent)
        .where(Agent.is_board_lead == True)  # noqa: E712
        .where(col(Agent.archived_at).is_(None))
        .order_by(col(Agent.created_at), col(Agent.name))
    )
    lead: Agent | None = None
    if board_id is not None:
        lead = (await session.exec(base.where(Agent.board_id == board_id).limit(1))).first()
    if lead is None:
        lead = (await session.exec(base.limit(1))).first()
    if lead is None:
        raise HTTPException(status_code=404, detail="no_lead")
    return VoiceLiveTarget(agent_id=lead.id, agent_name=lead.name)


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
    # Settings → Profile "Voice language" (operator request 13.10.2026): an
    # explicit preference overrides the UI locale in ``body.lang`` and also
    # sets the realtime transcription hint, so the voice speaks German even
    # when the UI itself is in English. "auto" (default) keeps following
    # the UI, exactly as before.
    preference = await voice_live.load_voice_language_preference(session, _user.id)
    lang, transcription_lang = voice_live.resolve_voice_language(preference, body.lang)
    try:
        minted = await voice_live.mint_client_secret(agent.name, lang, transcription_lang)
    except voice_live.VoiceLiveError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": {"reason": exc.reason}})
    return VoiceLiveSession(
        client_secret=minted.client_secret,
        expires_at=minted.expires_at,
        model=minted.model,
        voice=minted.voice,
        calls_url=minted.calls_url,
        agent_name=agent.name,
        call_ref=voice_live_tools.open_call(agent.id),
    )


# ── tools (voice PR 2) ───────────────────────────────────────────────────


class VoiceToolBody(BaseModel):
    call_ref: str = Field(min_length=1, max_length=128)
    agent_id: uuid.UUID
    name: Literal["send_to_agent", "get_agent_status", "stop_agent"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: The operator's own words, exactly as the speech-to-text heard them.
    #: ``send_to_agent``: everything since the last delivered send (required).
    #: ``stop_agent``: the operator's LAST utterance only (required) — MC
    #: re-checks that it is a stop command as a whole.
    transcript: str = Field(default="", max_length=voice_live_tools.MAX_TRANSCRIPT_CHARS)


class VoiceToolResult(BaseModel):
    #: What the browser hands the realtime model as ``function_call_output``.
    output: dict[str, Any]
    #: ``send_to_agent`` only: the exact chat message that was delivered, so the
    #: chat can echo it at once (the transcript confirms it moments later).
    sent_text: str | None = None
    #: ``send_to_agent`` only: was the agent mid-turn (echo shows "queued")?
    mid_turn: bool = False
    #: ``stop_agent`` only: the stop went out (the chat may drop its queued rows).
    stopped: bool = False
    #: ``stop_agent`` on a headless agent: the follow-ups the daemon held
    #: back, oldest first — the chat puts them back into the composer, like
    #: the Stop button. ``None`` = nothing was taken back (TUI, or no answer).
    taken_back: list[str] | None = None


def _tool_error(exc: VoiceToolError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": {"reason": exc.reason}})


# What the voice may say when a delivery fails. The text goes to the model,
# not to the screen; the screen gets the reason code.
_NOT_DELIVERED_SAY = {
    "agent_starting": "NOT delivered: the agent is still starting. Tell the operator to try again in a moment.",
    "input_not_supported": "NOT delivered: this agent cannot receive chat messages.",
    "acp_unreachable": "NOT delivered: the agent did not answer. Tell the operator it did not arrive.",
    "boss_delivery_failed": "NOT delivered: the agent did not answer. Tell the operator it did not arrive.",
    "agent_busy": "NOT delivered: the agent refused a message right now. Tell the operator to type it in the chat.",
    "acp_refused": "NOT delivered: the agent refused it. Tell the operator to type it in the chat.",
    "install_blocked": "NOT delivered: installs are blocked in the chat.",
    "approval_pending": (
        "NOT delivered: he waits for an approval or a choice in the chat. A spoken answer never "
        "counts. Ask the operator to tap it in the chat (\"bitte im Chat antippen\")."
    ),
}


async def _turn_status(agent: Agent) -> tuple[str | None, bool]:
    """``(status, headless)`` — ``working``/``idle``/``permission_prompt``/
    ``waiting_input`` or ``None`` (unknown). Read-only; never touches the agent."""
    from app.services import acp_chat_transport
    from app.services import transcript_chat

    headless = acp_chat_transport.headless_chat_kind(agent) is not None
    try:
        if headless:
            status = await asyncio.to_thread(acp_chat_transport.read_acp_turn_status, agent)
        else:
            status = (transcript_chat.tailer_manager.last_state(str(agent.id)) or {}).get("status")
    except Exception:  # noqa: BLE001 — a hint for the wording, never a blocker
        status = None
    return status, headless


async def _agent_is_mid_turn(agent: Agent) -> tuple[bool, bool]:
    """``(mid_turn, headless)``. Read-only; never touches the agent."""
    status, headless = await _turn_status(agent)
    return status == "working", headless


#: Prompt states where typing text + Enter picks an option in the TUI.
_PROMPT_STATES = frozenset({"permission_prompt", "waiting_input"})


async def _prompt_open(agent: Agent, status: str | None, headless: bool) -> bool:
    """Is a permission prompt or a choice menu open in this agent's TUI?
    A voice message ends with Enter; typed into such a prompt, Enter picks the
    highlighted option — an approval by voice (bauplan B2 rule 5). The
    tailer's state can lag a few seconds, so a docker agent's pane is also
    read fresh. Headless (ACP) agents queue text and never answer a prompt
    with it."""
    if headless:
        return False
    if status in _PROMPT_STATES:
        return True
    from app.services import pane_state, transcript_adapters

    try:
        pane = await pane_state.capture_pane(agent)
        if pane is None:
            return False
        fresh = transcript_adapters.adapter_for(agent).parse_pane_state(pane, False)["status"]
    except Exception:  # noqa: BLE001 — no pane, no prompt we could hit
        return False
    return fresh in _PROMPT_STATES


async def _send_to_agent(agent: Agent, call, body: VoiceToolBody) -> VoiceToolResult:
    from app.services import agent_chat_input as chat_input
    from app.services.acp_chat_transport import AcpChatUnreachableError

    text = body.arguments.get("text")
    if not isinstance(text, str) or not voice_live_tools.clean_line(text):
        raise VoiceToolError("text_missing", 422)
    if len(text) > voice_live_tools.MAX_TEXT_CHARS:
        raise VoiceToolError("text_too_long", 422)
    if not voice_live_tools.clean_line(body.transcript):
        # The operator's own words are what makes a voice message trustworthy
        # (incident 12.09.2026). Without them nothing goes out.
        return VoiceToolResult(
            output={
                "delivered": False,
                "reason": "no_transcript",
                "say": "NOT delivered: I did not catch the operator's exact words. Ask them to say it again.",
            }
        )
    if voice_live_tools.is_duplicate(call, text):
        return VoiceToolResult(
            output={
                "delivered": True,
                "duplicate": True,
                "done": False,
                "say": "Already passed on a moment ago. Do not send it again.",
            }
        )

    message = voice_live_tools.compose_message(text, body.transcript)
    status, headless = await _turn_status(agent)
    mid_turn = status == "working"
    reason: str | None = None
    if await _prompt_open(agent, status, headless):
        logger.info("voice_live tool send_to_agent agent=%s not delivered: approval_pending", agent.id)
        return VoiceToolResult(
            output={"delivered": False, "reason": "approval_pending", "say": _NOT_DELIVERED_SAY["approval_pending"]}
        )
    try:
        await chat_input.send_text(agent, message)
    except chat_input.InstallCommandBlockedError:
        reason = "install_blocked"
    except chat_input.InputNotSupportedError:
        reason = "input_not_supported"
    except chat_input.AgentStartingError:
        reason = "agent_starting"
    except chat_input.AgentBusyError:
        reason = "agent_busy"
    except chat_input.AcpChatRefusedError:
        reason = "acp_refused"
    except AcpChatUnreachableError:
        reason = "acp_unreachable"
    except chat_input.BossDeliveryError:
        reason = "boss_delivery_failed"
    if reason is not None:
        logger.info("voice_live tool send_to_agent agent=%s not delivered: %s", agent.id, reason)
        return VoiceToolResult(
            output={"delivered": False, "reason": reason, "say": _NOT_DELIVERED_SAY[reason]}
        )

    voice_live_tools.remember_send(call, text)
    if mid_turn and headless:
        when = "after_current_step"
        say = "Passed on. He is in the middle of a step; it comes after the current step. NOT done yet."
    elif mid_turn:
        when = "during_current_work"
        say = "Passed on. He is working and picks it up at his next step. NOT done yet."
    else:
        when = "now"
        say = "Passed on. He starts on it now. NOT done yet."
    logger.info(
        "voice_live tool send_to_agent agent=%s delivered len=%d when=%s", agent.id, len(message), when
    )
    return VoiceToolResult(
        output={"delivered": True, "when": when, "done": False, "say": say},
        sent_text=message,
        mid_turn=mid_turn,
    )


_STOP_SAY = {
    "no_transcript": (
        "NOT stopped: I did not catch the operator's exact words. Ask whether you should stop "
        "him; he stops only when they say \"Stopp\" on its own."
    ),
    "not_a_stop_command": (
        "NOT stopped: the operator said more than a stop command. If it is a request (for example "
        "\"Stopp den Container\"), pass it on with send_to_agent; if unsure, ask back."
    ),
    "not_working": "Nothing to stop: he is not working right now. Say so in one short sentence.",
    "failed": (
        "NOT stopped: the stop did not reach him. Tell the operator to tap the Stop button in the chat."
    ),
}


async def _wait_until_stopped(agent: Agent) -> bool:
    """Watches the turn end for a few seconds after a stop. True = seen idle
    (or prompt-free and not working); False = not confirmed in time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + voice_live_tools.STOP_CONFIRM_S
    while True:
        status, _headless = await _turn_status(agent)
        if status is not None and status != "working":
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(voice_live_tools.STOP_POLL_S)


async def _stop_agent(agent: Agent, body: VoiceToolBody) -> VoiceToolResult:
    """Stop by voice (voice PR 4): the operator's LAST utterance, verbatim,
    must be a stop command as a whole — the model calling the tool is not
    enough. Then the chat Stop button's own path: headless agents first hand
    back the follow-ups their daemon holds (they would start the moment the
    turn ends), then ACP ``cancel``; TUI agents get ``Escape`` (their
    interrupt key). "Gestoppt" only once the turn is seen to end."""
    from app.services import agent_chat_input as chat_input
    from app.services.acp_chat_transport import AcpChatUnreachableError

    words = voice_live_tools.clean_line(body.transcript)

    def refuse(reason: str) -> VoiceToolResult:
        logger.info("voice_live tool stop_agent agent=%s not stopped: %s", agent.id, reason)
        return VoiceToolResult(output={"stopped": False, "reason": reason, "say": _STOP_SAY[reason]})

    if not words:
        return refuse("no_transcript")
    if not voice_live_tools.is_stop_command(words, agent.name):
        return refuse("not_a_stop_command")
    status, headless = await _turn_status(agent)
    if status == "idle":
        return refuse("not_working")

    taken_back: list[str] | None = None
    if headless:
        try:
            taken_back = await chat_input.clear_queue(agent)
        except (chat_input.InputNotSupportedError, AcpChatUnreachableError):
            taken_back = None  # the daemon keeps them; the cancel goes out regardless
    try:
        await chat_input.send_keys(agent, ["Escape"])
    except (
        chat_input.InputNotSupportedError,
        AcpChatUnreachableError,
        chat_input.BossDeliveryError,
        ValueError,
    ) as exc:
        logger.info("voice_live tool stop_agent agent=%s failed: %s", agent.id, type(exc).__name__)
        result = refuse("failed")
        result.taken_back = taken_back
        return result

    confirmed = await _wait_until_stopped(agent)
    back = len(taken_back or [])
    back_note = (
        f" {back} queued message{'s' if back != 1 else ''} went back into the chat input, not sent."
        if back
        else ""
    )
    if confirmed:
        say = (
            "Stopped: his current step was cancelled; nothing was undone. Say only \"Gestoppt.\" "
            "(German) or \"Stopped.\" (English)." + back_note
        )
    else:
        say = (
            "Stop sent, but his turn has not ended yet. Do NOT say \"Gestoppt\" or \"stopped\". Say: "
            "\"Stopp ist raus, er hält gleich an.\" or \"Stop sent, he is halting.\"" + back_note
        )
    logger.info(
        "voice_live tool stop_agent agent=%s stopped confirmed=%s taken_back=%d", agent.id, confirmed, back
    )
    return VoiceToolResult(
        output={"stopped": True, "confirmed": confirmed, "taken_back": back, "say": say},
        stopped=True,
        taken_back=taken_back,
    )


async def _get_agent_status(agent: Agent, session: AsyncSession) -> VoiceToolResult:
    from app.routers.agent_chat import _resolve_transcript_path
    from app.services.checklist_todo_sync import chat_todo_sync
    from app.services.transcript_chat import read_history, tailer_manager

    state = tailer_manager.last_state(str(agent.id))
    events: list[dict[str, Any]] = []
    todo: dict[str, Any] | None = None
    try:
        resolved = await _resolve_transcript_path(agent.id, session)
        if not isinstance(resolved, JSONResponse):
            _agent, path, adapter = resolved
            history = await asyncio.to_thread(read_history, path, adapter, 40)
            events = history.get("events") or []
            todo = await chat_todo_sync.chat_todos(session, agent, adapter, history.get("todos"))
            if state is None:
                state = await tailer_manager._compute_pane_state(agent, path, adapter)
    except Exception:  # noqa: BLE001 — a status answer must not 500 the call
        logger.info("voice_live tool get_agent_status agent=%s: partial snapshot", agent.id, exc_info=True)
    return VoiceToolResult(output=voice_live_tools.build_status(state, todo, events))


@router.post("/tool", response_model=VoiceToolResult)
async def run_tool(
    body: VoiceToolBody,
    _user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """One tool call of a live voice call. Guards, in order: feature on,
    admin, the call is known and was opened for THIS agent, the agent still
    exists and is not archived, per-call rate limit, argument limits."""
    _require_enabled()
    try:
        call = voice_live_tools.check_call(body.call_ref, body.agent_id)
        agent = await session.get(Agent, body.agent_id)
        if agent is None:
            raise VoiceToolError("agent_not_found", 404)
        if getattr(agent, "archived_at", None) is not None:
            raise VoiceToolError("agent_archived", 409)
        voice_live_tools.take_rate(call, body.name)
        if body.name == "send_to_agent":
            return await _send_to_agent(agent, call, body)
        if body.name == "stop_agent":
            return await _stop_agent(agent, body)
        return await _get_agent_status(agent, session)
    except VoiceToolError as exc:
        return _tool_error(exc)


# ── progress context (voice PR 3) ────────────────────────────────────────


class _Strict(BaseModel):
    # An unknown field is refused, not dropped: a tool OUTPUT must never be
    # accepted here, not even to be thrown away (bauplan B4).
    model_config = {"extra": "forbid"}


class ProgressStep(_Strict):
    index: int = Field(ge=0, le=999)
    total: int = Field(ge=0, le=999)
    content: str = Field(default="", max_length=500)
    all_done: bool = False


class ProgressTool(_Strict):
    name: str = Field(max_length=80)
    title: str = Field(default="", max_length=500)
    failed: bool = False


class VoiceProgressBody(_Strict):
    call_ref: str = Field(min_length=1, max_length=128)
    agent_id: uuid.UUID
    kind: Literal[voice_progress.PROGRESS_KINDS]  # type: ignore[valid-type]
    step: ProgressStep | None = None
    tool: ProgressTool | None = None
    #: ``final`` only: the agent's own last reply (shortened server-side).
    text: str = Field(default="", max_length=8000)
    question: str = Field(default="", max_length=1000)
    error_code: str = Field(default="", max_length=64)
    elapsed_s: int | None = Field(default=None, ge=0, le=24 * 3600)


class VoiceProgressItem(BaseModel):
    #: The system item text the browser hands the realtime model.
    text: str
    #: True only for ``final``: the agent itself reported the end of its turn.
    agent_final: bool


@router.post("/progress", response_model=VoiceProgressItem)
async def progress_item(
    body: VoiceProgressBody,
    _user=Depends(require_role(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """One progress line for an open call. Same guards as ``/tool``: feature
    on, admin, call known and opened for THIS agent, per-call rate limit."""
    _require_enabled()
    try:
        call = voice_live_tools.check_call(body.call_ref, body.agent_id)
        agent = await session.get(Agent, body.agent_id)
        if agent is None:
            raise VoiceToolError("agent_not_found", 404)
        voice_live_tools.take_rate(call, "progress")
    except VoiceToolError as exc:
        return _tool_error(exc)
    item = voice_progress.progress_line(
        body.kind,
        agent.name,
        str(agent.id),
        step=body.step.model_dump() if body.step else None,
        tool=body.tool.model_dump() if body.tool else None,
        text=body.text,
        question=body.question,
        error_code=body.error_code,
        elapsed_s=body.elapsed_s,
    )
    return VoiceProgressItem(**item)
