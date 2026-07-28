"""Telegram as a ChatAdapter (ADR-072).

Everything Telegram-specific about the team chat, and nothing else. Like
``HermesAdapter`` in the host-harness registry (ADR-064) this is a thin shell
over the existing, unchanged Telegram services — the refactor moved the
channel-neutral rules OUT (into ``chat_outbound``/``chat_inbound``/
``chat_rooms``), it did not rewrite Telegram.

Two Telegram facts shape the implementation:

  * **One bot, one identity.** Every message comes from the same bot account,
    so ``capabilities.sender_identity`` is False and ``send`` degrades the
    ``ChatSender`` into the visible prefix ``"Rex: fertig"``. That is a
    *rendering* decision made here — the pipeline hands over identity as data.
  * **Topics are the rooms.** A forum topic id is the room ref; the general
    topic has none, for which ``telegram_topics.GENERAL_TOPIC_ID`` (0) is the
    sentinel that means "chat root".
"""
from __future__ import annotations

import logging

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.chat_adapter import (
    BaseChatAdapter,
    ChatCapabilities,
    ChatRoomRef,
    OutboundChatMessage,
)

logger = logging.getLogger("mc.chat_telegram")


class TelegramChatAdapter(BaseChatAdapter):
    key = "telegram"
    label = "Telegram"
    capabilities = ChatCapabilities(sender_identity=False, rooms=True)

    def __init__(self, *, topic_client=None, bot=None):
        """Transports are injectable so tests (and the legacy P2.3 entry point
        ``telegram_outbound.mirror_message_to_telegram``) can hand in fakes;
        omitted, the production singletons are used."""
        self._topic_client_override = topic_client
        self._bot_override = bot

    # ── Switch ───────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        from app.config import settings

        return bool(getattr(settings, "telegram_team_chat_enabled", False))

    def is_configured(self) -> bool:
        from app.config import settings

        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    # ── Transports ───────────────────────────────────────────────────────

    def _bot(self):
        if self._bot_override is not None:
            return self._bot_override
        from app.services.telegram_bot import telegram_bot

        return telegram_bot

    def _topic_client(self):
        if self._topic_client_override is not None:
            return self._topic_client_override
        from app.services.telegram_topics import TelegramForumClient

        return TelegramForumClient()

    # ── Rooms ────────────────────────────────────────────────────────────

    async def ensure_room(self, session: AsyncSession, thread) -> ChatRoomRef | None:
        from app.services.telegram_topics import ensure_topic_for_thread

        return await ensure_topic_for_thread(session, thread, self._topic_client())

    async def resolve_thread_for_room(self, session: AsyncSession, room: ChatRoomRef):
        from app.services.telegram_inbound import _thread_for_topic

        return await _thread_for_topic(session, int(room))

    async def handle_task_done(self, session: AsyncSession, task) -> None:
        # Module attribute (not a from-import): the topic module owns Marks
        # rules about WHICH owner may be marked done, and stays the single
        # place they live.
        from app.services import telegram_topics

        await telegram_topics.handle_task_done(session, task)

    async def purge_rooms(self, older_than_days: int) -> int:
        from app.services import telegram_topics

        return await telegram_topics.purge_topics_tick(older_than_days=older_than_days)

    # ── Messages ─────────────────────────────────────────────────────────

    def render_sender_prefix(self, message: OutboundChatMessage) -> str:
        """Telegram's identity degradation: one bot account cannot carry a
        per-message sender, so the name goes into the text. Visible, never
        dropped. ``sender is None`` = the bot speaks as itself (no prefix)."""
        if message.sender is None:
            return message.body
        return f"{message.sender.display_name}: {message.body}"

    async def send(
        self, room: ChatRoomRef | None, message: OutboundChatMessage
    ) -> bool:
        from app.services.telegram_topics import GENERAL_TOPIC_ID

        # GENERAL_TOPIC_ID (0) -> ohne message_thread_id (Chat-Stamm). send_message
        # laesst den falsy Wert ohnehin weg; wir sind hier explizit.
        thread_arg = None if room in (None, GENERAL_TOPIC_ID) else room
        try:
            await self._bot().send_message(
                self.render_sender_prefix(message),
                message_thread_id=thread_arg,
                disable_notification=message.silent,
            )
            return True
        except Exception as e:  # noqa: BLE001 — ein Sendefehler darf nie werfen
            logger.warning("telegram send failed: %s", e)
            return False

    async def mirror_message(self, session: AsyncSession, message, *, now=None) -> bool:
        """Telegram's outbound entry point.

        Overridden (the base runs the neutral pipeline directly) so the P2.3
        module function stays the one place that wires Telegram's transports
        into the pipeline — it is the documented injection point for tests and
        for anything that already imports it.
        """
        from app.services import telegram_outbound

        return await telegram_outbound.mirror_message_to_telegram(
            session,
            message,
            topic_client=self._topic_client(),
            bot=self._bot(),
            now=now,
        )
