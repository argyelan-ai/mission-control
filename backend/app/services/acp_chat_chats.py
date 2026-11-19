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
* Claude Code agents are not wired yet (their start script does not take a
  per-chat ``--session-id`` / ``--resume``); they keep today's behaviour.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services import chat_record
from app.services.acp_chat_transport import headless_chat_kind


class ChatNotFoundError(Exception):
    """The chat id names no visible, unarchived chat of this agent."""


class ChatRecordDisabledError(Exception):
    """A chat id was sent while the record flag is off."""


def further_chats_supported(agent: Any) -> bool:
    """True when this agent's daemon can hold further chats: the record is
    on and the agent chats over ACP in its omp container."""
    return (
        bool(getattr(settings, "chat_record_enabled", False))
        and headless_chat_kind(agent) == "acp-docker"
        and chat_record.harness_name(agent) == "omp"
    )


async def resolve_chat(session: AsyncSession, user: Any, agent: Any, chat_id: uuid.UUID | None) -> str | None:
    """The chat id to hand to the daemon, or None for the agent's own chat.

    ``ChatRecordDisabledError`` when a chat id came while the flag is off;
    ``ChatNotFoundError`` when it is not a visible, unarchived chat of
    ``agent`` (another agent's chat is "not found", never routed). The
    agent's default chat is the daemon's default session — None."""
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
    if not further_chats_supported(agent):
        raise ChatNotFoundError()
    return str(chat.id)


async def new_chat_for_reset(session: AsyncSession, user: Any, agent: Any, text: str) -> dict | None:
    """``/new`` or ``/clear`` with the record on: a further chat of the agent
    (committed), as ``chat_record.chat_out``. None when the text is no reset
    MC handles here — any other text, the agent's OWN command of that name
    (it goes to the harness as text, as before), or an agent / flag state
    that keeps today's reset."""
    if not further_chats_supported(agent):
        return None
    from app.services.agent_chat_input import _acp_session_reset_command

    reset = await _acp_session_reset_command(agent, text)
    if reset is None or reset[2]:
        return None
    chat = await chat_record.create_chat(session, user, agent)
    await session.commit()
    return chat_record.chat_out(chat)
