"""Further chats of a headless omp agent on its chat daemon (chats slice S2).

The record side (tables, pin and resume routes) is ``chat_record`` (S2a).
This module is the chat-input side: which chat a composer message goes to,
and what ``/new`` means once the record is on.

* ``CHAT_RECORD_ENABLED`` off: nothing here changes anything — no chat id is
  accepted, ``/new`` stays the daemon's ``new_session`` (a fresh session in
  the agent's one chat), exactly as before.
* Flag on, an omp agent whose chat runs over ACP in its container
  (``headless_chat_kind == "acp-docker"``): a message may name a further chat
  of that agent (``chatId``); the daemon runs it in that chat's own native
  session (``docker/omp-bridge/acp_chat_pool.py``). ``/new`` / ``/clear``
  creates a further chat in the record instead of replacing the session of
  the chat it was typed in — the old conversation stays its own chat. The
  daemon opens the new chat's session on its first message (``session/new``
  + pin through the agent-token route).
* Flag on, a Claude Code container agent (cli-bridge, not headless): a
  message may name a further Claude chat of that agent; ``resolve_chat``
  hands its id on and ``agent_chat_input.send_text`` puts the text into that
  chat's own claude process (``claude_chat_runner``, chats slice S3 item A).
* Routing is by the CHAT's harness (``chats.harness``): an omp chat goes to
  the omp daemon, a Claude chat to the Claude chat runner — each only on an
  agent that can run it. A chat whose harness this agent cannot run is
  refused (``ChatHarnessUnsupportedError``), never sent to the wrong process.
* Switching a further chat's pair (S3 item C, ADR-105): :class:`OmpChatDriver`
  is ``chat_switch``'s driver for harness ``omp``. It stops a running turn
  through the daemon (``stop_for_switch``: the daemon holds the queue for the
  switch, open question cards end ``expired_by_switch``), changes the model of the
  chat's own session (``config model``), and starts the switch
  (``switch``: ``session/load`` first when MC says to try it, else
  ``session/new`` with the handoff package as the first prompt's
  ``context``). The held queue runs after the handoff turn in the daemon
  (rule F8); whatever an older daemon still hands back is sent again with
  ``mode=queue`` right after.
  The daemon reports the outcome on the switch result route. The agent's
  default chat keeps ``chat_switch.AgentDefaultDriver``.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services import chat_record
from app.services.acp_chat_transport import AcpChatUnreachableError, headless_chat_kind

logger = logging.getLogger(__name__)


class ChatNotFoundError(Exception):
    """The chat id names no visible, unarchived chat of this agent."""


class ChatRecordDisabledError(Exception):
    """A chat id was sent while the record flag is off."""


class ChatHarnessUnsupportedError(Exception):
    """A chat of this agent on a harness this agent cannot run as a further
    chat (e.g. a Claude chat left on an agent that now runs omp)."""

    def __init__(self, harness: str):
        super().__init__(harness)
        self.harness = harness


def further_chats_supported(agent: Any) -> bool:
    """True when this agent's daemon can hold further chats: the record is
    on and the agent chats over ACP in its omp container."""
    return (
        bool(getattr(settings, "chat_record_enabled", False))
        and headless_chat_kind(agent) == "acp-docker"
        and chat_record.harness_name(agent) == "omp"
    )


def claude_chats_supported(agent: Any) -> bool:
    """True when a further Claude chat of this agent can run: the record is
    on and the agent is a Claude Code container agent (cli-bridge, not
    headless). The same rule ``agent_chat_input.send_text`` applies before
    it opens the chat's process (``claude_chat_runner.open_chat``)."""
    return (
        bool(getattr(settings, "chat_record_enabled", False))
        and getattr(agent, "agent_runtime", None) == "cli-bridge"
        and headless_chat_kind(agent) is None
        and (getattr(agent, "harness", None) or "claude") == "claude"
    )


#: Which agents can run a further chat, per chat harness.
_FURTHER_CHAT_SUPPORT = {
    "omp": further_chats_supported,
    "claude": claude_chats_supported,
}


def can_start_chat(agent: Any) -> bool:
    """True when a new further chat of this agent could run at all — on the
    agent's own harness, under the same rule ``resolve_chat`` routes by. The
    UI reads this (``Agent.can_start_chat``) instead of comparing harness
    names itself (PRINCIPLES §3.4)."""
    return any(supported(agent) for supported in _FURTHER_CHAT_SUPPORT.values())


async def resolve_chat(session: AsyncSession, user: Any, agent: Any, chat_id: uuid.UUID | None) -> str | None:
    """The chat id to hand to the daemon, or None for the agent's own chat.

    ``ChatRecordDisabledError`` when a chat id came while the flag is off;
    ``ChatNotFoundError`` when it is not a visible, unarchived chat of
    ``agent`` (another agent's chat is "not found", never routed). The
    agent's default chat is the daemon's default session — None.
    ``ChatHarnessUnsupportedError`` when the chat's harness is not one this
    agent can run as a further chat (routing is by the chat's harness: omp →
    the chat daemon, Claude → ``claude_chat_runner`` via ``send_text``)."""
    if chat_id is None:
        return None
    if not getattr(settings, "chat_record_enabled", False):
        raise ChatRecordDisabledError()
    chat = await chat_record.get_chat(session, user, chat_id)
    if (
        chat is None
        or chat.legacy_agent_id != getattr(agent, "id", None)
        or chat.archived_at is not None
    ):
        raise ChatNotFoundError()
    if chat.agent_default:
        return None
    harness = chat.harness or chat_record.harness_name(agent)
    supported = _FURTHER_CHAT_SUPPORT.get(harness)
    if supported is None or not supported(agent):
        raise ChatHarnessUnsupportedError(harness)
    return str(chat.id)


async def new_chat_for_reset(session: AsyncSession, user: Any, agent: Any, text: str) -> tuple[dict, str] | None:
    """``/new`` or ``/clear`` with the record AND ``chat_record_new_as_chat``
    on: a further chat of the agent (committed), as ``chat_record.chat_out``,
    and the command (``"/new"``). None when the text is no reset MC handles
    here — any other text, the agent's OWN command of that name (it goes to
    the harness as text, as before), or an agent / flag state that keeps
    today's reset. The sub-flag stays off until the chat view follows
    ``newChat``: before that the new chat could not be reached and ``/new``
    would do less than today."""
    if not getattr(settings, "chat_record_new_as_chat", False):
        return None
    if not further_chats_supported(agent):
        return None
    from app.services.agent_chat_input import _acp_session_reset_command

    reset = await _acp_session_reset_command(agent, text)
    if reset is None or reset[2]:
        return None
    chat = await chat_record.create_chat(session, user, agent)
    await session.commit()
    return chat_record.chat_out(chat), f"/{reset[0]}"


# ── Switch driver (S3 item C, ADR-105) ──────────────────────────────────

#: A queue ``stop_for_switch`` handed back is sent again after the switch.
#: When no switch or model change follows (the request was refused after the
#: stop — ``box_locked``, a lost race), it goes back to the chat after this
#: long instead: never dropped.
HELD_REPLAY_AFTER_S = 30.0


class OmpChatDriver:
    """``chat_switch.SwitchDriver`` for the further chats of an omp agent
    whose chat runs on the container daemon (``further_chats_supported``).

    Limits, said out loud instead of pretended (reported as
    ``process_start_failed`` with the reason): the new session runs on the
    chat's own agent and its runtime — the daemon has one ``models.yml``
    per agent, so another runtime than the agent's (``other_runtime``) and
    a chat whose agent is no omp chat daemon (``no_chat_daemon``, e.g. a
    Claude chat moving to omp) are refused."""

    def __init__(self) -> None:
        #: chat id -> (agent slug, held jobs) from ``stop_for_switch``.
        self._held: dict[str, tuple[str, list[dict]]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    @staticmethod
    def _default():
        from app.services import chat_switch

        return chat_switch._DEFAULT_DRIVER

    @staticmethod
    def _transport(agent: Any, chat: Any):
        from app.services import chat_switch
        from app.services.acp_chat_transport import DockerCtlTransport

        if agent is None or not further_chats_supported(agent):
            raise chat_switch.DriverUnavailable("no_chat_daemon")
        return DockerCtlTransport(agent.slug, chat_id=str(chat.id))

    async def is_busy(self, agent: Any, chat: Any) -> bool | None:
        if chat.agent_default:
            return await self._default().is_busy(agent, chat)
        if agent is None or not further_chats_supported(agent):
            return False  # nothing of this chat runs on a daemon
        try:
            state = await self._transport(agent, chat).state()
        except AcpChatUnreachableError:
            return None
        if state.get("opening"):
            return True  # its first message is about to run
        return state.get("busy") is True

    async def stop_turn(self, agent: Any, chat: Any) -> None:
        from app.services import chat_switch

        if chat.agent_default:
            await self._default().stop_turn(agent, chat)
            return
        try:
            answer = await self._transport(agent, chat).stop_for_switch()
        except AcpChatUnreachableError as exc:
            raise chat_switch.DriverUnavailable("chat_daemon_unreachable") from exc
        if not answer.get("ok"):
            raise chat_switch.DriverUnavailable(str(answer.get("error") or "stop_refused"))
        self._hold(agent.slug, str(chat.id), answer.get("handedOver"))

    async def set_model(self, agent: Any, chat: Any, model: str) -> None:
        from app.services import chat_switch

        if chat.agent_default:
            await self._default().set_model(agent, chat, model)
            return
        transport = self._transport(agent, chat)
        try:
            answer = await transport.config("model", model)
        except AcpChatUnreachableError as exc:
            raise chat_switch.DriverUnavailable("chat_daemon_unreachable") from exc
        finally:
            await self._replay(agent.slug, str(chat.id))
        if not answer.get("ok"):
            # The session refused the model (not offered): chat_switch
            # records ``model_not_offered``.
            raise ValueError(str(answer.get("detail") or answer.get("error") or "refused"))

    async def start(self, agent: Any, chat: Any, request: dict[str, Any]) -> None:
        from app.services import chat_switch

        if chat.agent_default:
            await self._default().start(agent, chat, request)
            return
        to = request.get("to") or {}
        if to.get("harness") != "omp":
            raise chat_switch.DriverUnavailable("not_an_omp_switch")
        transport = self._transport(agent, chat)
        runtime = to.get("runtimeId")
        own = getattr(agent, "runtime_id", None)
        if runtime and str(runtime) != str(own or ""):
            # One models.yml per agent: the daemon cannot reach another
            # runtime's endpoint for one chat (open point of item C).
            await self._replay(agent.slug, str(chat.id))
            raise chat_switch.DriverUnavailable("other_runtime")
        payload: dict[str, Any] = {
            "switchEventId": request["switchEventId"],
            "action": request["action"],
            "tryNativeLoad": bool(request.get("tryNativeLoad")),
        }
        if request.get("nativeSessionId"):
            payload["nativeSessionId"] = request["nativeSessionId"]
        if request.get("package"):
            payload["package"] = request["package"]
        if to.get("model"):
            payload["model"] = to["model"]
        try:
            answer = await transport.switch(payload)
        except AcpChatUnreachableError as exc:
            await self._replay(agent.slug, str(chat.id))
            raise chat_switch.DriverUnavailable("chat_daemon_unreachable") from exc
        if not answer.get("ok"):
            await self._replay(agent.slug, str(chat.id))
            raise chat_switch.DriverUnavailable(str(answer.get("error") or "switch_refused"))
        # F8: what MC holds, then what the daemon still held — after the
        # handoff turn, in order.
        self._hold(agent.slug, str(chat.id), answer.get("handedOver"))
        await self._replay(agent.slug, str(chat.id))

    # -- the held queue ----------------------------------------------------

    def _hold(self, slug: str, chat_id: str, jobs: Any) -> None:
        jobs = [j for j in (jobs or []) if isinstance(j, dict) and isinstance(j.get("text"), str)]
        if not jobs:
            return
        _, held = self._held.get(chat_id, (slug, []))
        self._held[chat_id] = (slug, [*held, *jobs])
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        old = self._timers.pop(chat_id, None)
        if old is not None:
            old.cancel()
        self._timers[chat_id] = loop.call_later(
            HELD_REPLAY_AFTER_S, lambda: asyncio.ensure_future(self._replay(slug, chat_id))
        )

    async def _replay(self, slug: str, chat_id: str) -> None:
        """Sends the held jobs again, oldest first, ``mode=queue``: they wait
        behind a running (handoff) turn. A refused or unreachable send is
        logged with its text count — never silently gone."""
        from app.services.acp_chat_transport import DockerCtlTransport

        timer = self._timers.pop(chat_id, None)
        if timer is not None:
            timer.cancel()
        _, jobs = self._held.pop(chat_id, (slug, []))
        if not jobs:
            return
        transport = DockerCtlTransport(slug, chat_id=chat_id)
        for i, job in enumerate(jobs):
            try:
                answer = await transport.prompt(
                    job["text"], mode="queue", origin=job.get("origin"), context=job.get("context"),
                )
            except AcpChatUnreachableError:
                answer = {"ok": False, "error": "unreachable"}
            if not answer.get("ok"):
                logger.error(
                    "chat %s: %d held message(s) not sent again after a switch (%s)",
                    chat_id, len(jobs) - i, answer.get("error"),
                )
                return


def register_switch_driver() -> OmpChatDriver:
    """Plugs the omp side into ``chat_switch`` (ADR-105 §6: a harness is a
    registry entry). Runs at import; the app imports this module through
    ``routers/agent_chat``."""
    from app.services import chat_switch

    driver = OmpChatDriver()
    chat_switch.register_driver("omp", driver)
    return driver


SWITCH_DRIVER = register_switch_driver()
