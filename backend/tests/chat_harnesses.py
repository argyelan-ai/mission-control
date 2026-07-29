"""Test harnesses for the ChatAdapter TCK (ADR-072).

Onboarding a chat channel = registering its adapter in
``services/chat_adapter.py`` AND adding a harness here. The TCK
(``test_chat_adapter_tck.py``) parametrises itself over ``CHAT_HARNESSES`` and
fails when a registered adapter has no harness — so a new channel cannot slip
past the conformance suite unnoticed. Same idea as the pane-fixture
directories of the runtime-adapter TCK (ADR-071), adapted from "record real
output" to "fake the transport", because a chat channel's transport is an HTTP
API, not a terminal.

A harness gives the TCK four things:
  * an adapter instance wired to a FAKE transport (no network, ever),
  * the record of what that transport was asked to send,
  * a way to bind an MC thread to a room (so inbound routing is testable),
  * a way to break the transport (so degradation is testable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.services.chat_adapter import ChatAdapter


@dataclass
class SentRecord:
    """One message the fake transport was asked to deliver.

    ``sender_name`` is only set by channels that can carry identity per
    message; channels that cannot must have degraded it INTO ``text``.
    """

    room: Any
    text: str
    silent: bool
    sender_name: str | None = None


@dataclass
class ChatHarness:
    key: str
    adapter: ChatAdapter
    sent: list[SentRecord]
    #: make the channel's switch + credentials say "on" (takes monkeypatch)
    enable: Callable[[Any], None]
    #: give this thread a room on the channel; returns the room ref
    bind_room: Callable[..., Any]  # async (session, thread) -> room ref
    #: a room ref this channel would never know
    unknown_room: Any
    #: make every transport call fail from now on
    break_transport: Callable[[], None]
    fakes: dict = field(default_factory=dict)


# ── Telegram ──────────────────────────────────────────────────────────────


class _FakeTelegramBot:
    def __init__(self, sent: list[SentRecord]):
        self._sent = sent
        self.broken = False

    async def send_message(self, text, *, message_thread_id=None, disable_notification=False):
        if self.broken:
            raise RuntimeError("telegram transport down")
        self._sent.append(
            SentRecord(room=message_thread_id, text=text, silent=disable_notification)
        )
        return 1


class _FakeForumClient:
    def __init__(self, next_id: int = 900):
        self._next_id = next_id
        self.broken = False

    async def create_forum_topic(self, name: str) -> int:
        if self.broken:
            from app.services.telegram_topics import TelegramTopicError

            raise TelegramTopicError("createForumTopic down")
        self._next_id += 1
        return self._next_id

    async def edit_forum_topic(self, message_thread_id: int, name: str) -> None:
        if self.broken:
            from app.services.telegram_topics import TelegramTopicError

            raise TelegramTopicError("editForumTopic down")

    async def delete_forum_topic(self, message_thread_id: int) -> None:
        if self.broken:
            from app.services.telegram_topics import TelegramTopicError

            raise TelegramTopicError("deleteForumTopic down")


def _telegram_harness() -> ChatHarness:
    from app.services.chat_telegram import TelegramChatAdapter

    sent: list[SentRecord] = []
    bot = _FakeTelegramBot(sent)
    topics = _FakeForumClient()
    adapter = TelegramChatAdapter(topic_client=topics, bot=bot)

    def enable(monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
        monkeypatch.setattr(settings, "telegram_bot_token", "test-token", raising=False)
        monkeypatch.setattr(settings, "telegram_chat_id", "4711", raising=False)

    async def bind_room(session, thread, room=4242):
        thread.telegram_topic_id = room
        session.add(thread)
        await session.commit()
        await session.refresh(thread)
        return room

    def break_transport():
        bot.broken = True
        topics.broken = True

    return ChatHarness(
        key="telegram",
        adapter=adapter,
        sent=sent,
        enable=enable,
        bind_room=bind_room,
        unknown_room=987654,
        break_transport=break_transport,
        fakes={"bot": bot, "topics": topics},
    )


# ── Slack ─────────────────────────────────────────────────────────────────


class _FakeSlackTransport:
    """Stands in for ``slack_client.SlackTransport``. No network, no token.

    Records exactly what Slack would have been asked for, including the
    per-message identity (``username`` / ``icon_emoji``) that is the whole
    point of this channel.
    """

    def __init__(self, sent: list[SentRecord]):
        self._sent = sent
        self.broken = False
        self.calls: list[dict] = []
        self._next_ts = 1_753_699_200

    async def post_message(
        self, *, channel, text, username=None, icon_emoji=None,
        thread_ts=None, silent=True,
    ):
        from app.services.slack_client import SlackPostResult

        self.calls.append(
            {
                "channel": channel, "text": text, "username": username,
                "icon_emoji": icon_emoji, "thread_ts": thread_ts, "silent": silent,
            }
        )
        if self.broken:
            # Slack's own failure shape — never an exception (the transport
            # swallows httpx errors), so this is what the adapter really sees.
            return SlackPostResult(
                ok=False, code="not_in_channel",
                error="The bot is not a member of that channel (not_in_channel).",
            )
        self._next_ts += 1
        self._sent.append(
            SentRecord(room=thread_ts, text=text, silent=silent, sender_name=username)
        )
        # Every post has a ts; the one from a post WITHOUT thread_ts is what
        # ensure_room stores as the room ref (that is how Slack threads work).
        return SlackPostResult(ok=True, ts=f"{self._next_ts}.000100")


class _StaticFaces:
    """The TCK cares that identity ARRIVES, not which emoji it wears —
    `test_chat_slack.py` tests the real resolver against the DB."""

    async def face_for(self, sender):
        return ":robot_face:"


def _slack_harness() -> ChatHarness:
    from app.services.chat_slack import SlackChatAdapter

    sent: list[SentRecord] = []
    transport = _FakeSlackTransport(sent)
    adapter = SlackChatAdapter(transport=transport, faces=_StaticFaces())

    def enable(monkeypatch):
        from app.config import settings
        from app.services import slack_client

        monkeypatch.setattr(settings, "slack_team_chat_enabled", True, raising=False)
        monkeypatch.setattr(settings, "slack_default_channel", "C-TEST", raising=False)
        # A token the adapter's sync gate can see, without a DB read.
        monkeypatch.setattr(
            slack_client, "_token_cache", (float("inf"), "xoxb-TEST-token"), raising=False
        )

    async def bind_room(session, thread, room="1753699200.001900"):
        thread.slack_thread_ts = room
        session.add(thread)
        await session.commit()
        await session.refresh(thread)
        return room

    def break_transport():
        transport.broken = True

    return ChatHarness(
        key="slack",
        adapter=adapter,
        sent=sent,
        enable=enable,
        bind_room=bind_room,
        unknown_room="9999999999.999999",
        break_transport=break_transport,
        fakes={"transport": transport},
    )


#: key -> factory. One entry per registered ChatAdapter (enforced by the TCK).
CHAT_HARNESS_FACTORIES: dict[str, Callable[[], ChatHarness]] = {
    "telegram": _telegram_harness,
    "slack": _slack_harness,
}
