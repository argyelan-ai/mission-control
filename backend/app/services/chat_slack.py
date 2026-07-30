"""Slack as a ChatAdapter (ADR-072) — the channel where agents have faces.

Everything Slack-specific about the team chat, and nothing else: the neutral
rules (what gets mirrored, how loud, loop protection, night quiet hours) live
in ``chat_outbound``/``chat_inbound``/``chat_rooms`` and are used, not rebuilt.

Three Slack facts shape this file:

  * **Per-message identity.** Unlike Telegram (one bot, so the agent name could
    only ever be a text prefix), ``chat.postMessage`` takes ``username`` and an
    icon per message — with the ``chat:write.customize`` scope. So
    ``capabilities.sender_identity`` is True and MC posts *as* Boss, *as* Rex,
    *as* FreeCode. No prefix. That is the whole reason the operator moved.

  * **The face is an emoji NAME, not an emoji.** ``icon_emoji`` takes a
    colon-code (``:mag:``) — Slack documents no raw-unicode form. Verified
    live against the operator's workspace on 2026-07-29: three messages to
    #mission-control with ``username`` + ``icon_emoji`` were accepted and came
    back with the set name (``subtype=bot_message``), and the operator saw
    three distinguishable senders in his client. The alternative ``icon_url``
    was rejected for a structural reason: Slack fetches that URL from *its
    own* servers, and MC is self-hosted behind Tailscale with no public URL —
    a generated avatar would be unreachable for Slack every time. Hence
    `agent_face()` below: colon-codes, never empty. Should real profile
    pictures ever be wanted, only this function changes.

  * **Rooms are message threads.** A "room" here is a ``thread_ts`` inside the
    default channel: one MC thread = one Slack thread = one task conversation.
    The general chat (``kind="dm"``) deliberately has no thread — it speaks in
    the channel itself, exactly like Telegram's general topic. One channel per
    project comes later; when it does, only *which channel* the parent message
    goes to changes, not this shape.
"""
from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Project
from app.models.task import Task
from app.models.thread import Thread
from app.services.chat_adapter import (
    BaseChatAdapter,
    ChatCapabilities,
    ChatRoomRef,
    ChatSender,
    OutboundChatMessage,
)

logger = logging.getLogger("mc.chat_slack")

# The general chat has no thread of its own — it is the channel itself. Same
# role as telegram_topics.GENERAL_TOPIC_ID (0): a sentinel that means "channel
# root", distinct from None ("channel not ready, skip this message").
GENERAL_ROOM = ""

_TITLE_MAX = 120


# ── Agent faces ───────────────────────────────────────────────────────────
#
# The operator picked ONE emoji per agent over generated avatars or uploads:
# instantly distinguishable, nothing to maintain, and it works for an agent
# somebody else creates in their own MC. `agents.emoji` already exists and is
# already shown in the UI, so this reuses it instead of adding a column.
#
# Resolution order (`agent_face`), first hit wins:
#   1. `agents.emoji` holding a colon-code (":t-rex:") — a deliberate override,
#      passed through verbatim so even a custom workspace emoji works.
#   2. `agents.emoji` holding a unicode emoji — translated to Slack's name.
#   3. the agent's role — a reviewer looks like a reviewer.
#   4. a stable hash of the agent's slug — a brand-new agent still gets its own
#      recognisable face instead of everyone sharing the default.
#   5. :robot_face:, so nothing ever posts faceless.

_COLON_CODE = re.compile(r"^:[a-z0-9][a-z0-9_+\-']*:$")

#: Unicode → Slack emoji name, covering every emoji MC's own agent templates
#: and seeds hand out, plus the obvious neighbours. An emoji that is not in
#: here is not lost — resolution simply falls through to the role.
_UNICODE_TO_NAME: dict[str, str] = {
    "👑": "crown",
    "🤖": "robot_face",
    "🎯": "dart",
    "🧑‍💻": "technologist",
    "🛡️": "shield",
    "📋": "clipboard",
    "🔍": "mag",
    "✍️": "writing_hand",
    "👀": "eyes",
    "⚡": "zap",
    "🚀": "rocket",
    "🦖": "t-rex",
    "🎨": "art",
    "🧪": "test_tube",
    "🔧": "wrench",
    "⚙️": "gear",
    "📦": "package",
    "🧠": "brain",
    "📚": "books",
    "✨": "sparkles",
    "🔮": "crystal_ball",
    "🛰️": "satellite",
    "🧭": "compass",
    "📡": "satellite_antenna",
    "🐙": "octopus",
    "🦉": "owl",
    "🐝": "bee",
    "🌟": "star2",
    "🔥": "fire",
    "💡": "bulb",
    "🖋️": "lower_left_fountain_pen",
    "🗂️": "card_index_dividers",
}

#: Role → face. Roles come from `app.scopes.AgentRole`; an unknown or missing
#: role falls through to the hash palette. orchestrator/developer/reviewer are
#: the three the operator saw and approved in the live check (Boss :dart:,
#: FreeCode :zap:, Rex :mag:) — derived from the role, so a newly created agent
#: gets a fitting face without anyone configuring one.
_ROLE_TO_NAME: dict[str, str] = {
    "lead": "crown",
    "orchestrator": "dart",
    "developer": "zap",
    "reviewer": "mag",
    "tester": "test_tube",
    "planner": "clipboard",
    "researcher": "books",
    "deployer": "rocket",
    "writer": "writing_hand",
    "relay": "satellite_antenna",
}

#: Last resort before the default: distinct, friendly, unmistakably different
#: from each other at 20px. Chosen by a stable hash, so an agent keeps its face
#: forever (and across restarts) without anything being stored.
_FACE_PALETTE: tuple[str, ...] = (
    "fox_face", "owl", "bee", "dolphin", "penguin", "koala",
    "turtle", "whale", "butterfly", "mushroom", "cactus", "maple_leaf",
)

DEFAULT_FACE = ":robot_face:"

# Slack renders "System" messages (watchdog, lifecycle) — they are not an agent
# and have no row to read a face from.
SYSTEM_FACE = ":gear:"


def _normalise_emoji(raw: str) -> str:
    """Drop the variation selector so "🛡️" (U+FE0F) and "🛡" are one key.

    Both spellings occur in the wild — an emoji picker usually emits the VS16
    form, a hand-typed one often does not — and they must not resolve to two
    different faces.
    """
    return raw.strip().replace("️", "")


#: The lookup table with both spellings folded into one.
_NORMALISED_UNICODE: dict[str, str] = {
    _normalise_emoji(k): v for k, v in _UNICODE_TO_NAME.items()
}


def agent_face(*, emoji: str | None, role: str | None, key: str) -> str:
    """The Slack colon-code this agent posts under. Never empty.

    ``key`` is the agent's stable slug (or name) — it only decides the hashed
    fallback, so an agent that is renamed but keeps its slug keeps its face.
    """
    raw = (emoji or "").strip()
    if _COLON_CODE.match(raw):
        return raw
    if raw:
        name = _NORMALISED_UNICODE.get(_normalise_emoji(raw))
        if name:
            return f":{name}:"
    role_name = _ROLE_TO_NAME.get((role or "").strip().lower())
    if role_name:
        return f":{role_name}:"
    if key:
        digest = hashlib.sha256(key.strip().lower().encode("utf-8")).digest()
        return f":{_FACE_PALETTE[digest[0] % len(_FACE_PALETTE)]}:"
    return DEFAULT_FACE


class SlackFaces:
    """Resolves ``ChatSender`` → Slack colon-code, with a small cache.

    ``ChatAdapter.send()`` takes no DB session on purpose (the neutral pipeline
    hands over a room and a message, not a transaction), but the face lives on
    the agent row. So this opens its own short-lived session and remembers the
    answer — an agent's emoji changes about never, and a chat line must not
    cost a query.

    ``session_factory`` is injectable so tests can hand in the test session
    instead of the production engine.
    """

    def __init__(self, session_factory=None):
        self._session_factory = session_factory
        self._cache: dict[str, str] = {}

    def forget(self) -> None:
        self._cache.clear()

    def _factory(self):
        if self._session_factory is not None:
            return self._session_factory()
        from app.database import async_session_maker

        return async_session_maker()

    async def face_for(self, sender: ChatSender) -> str:
        if sender.kind == "system":
            return SYSTEM_FACE
        if sender.agent_id is None:
            return DEFAULT_FACE
        cache_key = str(sender.agent_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            async with self._factory() as session:
                agent = (
                    await session.exec(select(Agent).where(Agent.id == sender.agent_id))
                ).one_or_none()
        except Exception as exc:  # noqa: BLE001 — a face is never worth an outage
            logger.warning("slack face lookup failed: %s", type(exc).__name__)
            return DEFAULT_FACE

        if agent is None:
            # No row (deleted agent, or a sender id from elsewhere): still give
            # it a stable face from the name we do have.
            return agent_face(emoji=None, role=None, key=sender.display_name)

        face = agent_face(
            emoji=agent.emoji,
            role=agent.role,
            key=agent.slug or agent.name or str(agent.id),
        )
        self._cache[cache_key] = face
        return face


# ── Markdown -> mrkdwn ────────────────────────────────────────────────────
#
# Agents write Markdown — MC's whole comment culture is Markdown, and that is
# correct. Slack renders its own dialect (mrkdwn): single *bold*, _italic_,
# <url|label> links, and it shows standard Markdown literally. The operator
# saw every heading of Boss's replies wrapped in raw asterisks (2026-07-30).
# The translation is THIS adapter's job (ADR-072: channel quirks live in the
# channel module) — teaching thirteen agents a per-channel dialect would be
# the drift machine all over again.
#
# Code is the sacred exception: anything inside backticks or fences passes
# through untouched — a transformed code sample is worse than an ugly one.

_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_BOLD_U = re.compile(r"__(.+?)__", re.S)
_MD_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?![*\w])")
_MD_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

# Placeholder that survives the italic pass: `*` may not appear in it, and it
# must be unlikely in real prose. NUL is both.
_BOLD_TOKEN = "\x00B\x00"


def _prose_to_mrkdwn(text: str) -> str:
    # Bold first, via a token: `**laut**` must not be re-read as italic once
    # its asterisks are single. Then italic, then the token resolves to
    # Slack's single-asterisk bold.
    text = _MD_BOLD.sub(lambda m: f"{_BOLD_TOKEN}{m.group(1)}{_BOLD_TOKEN}", text)
    text = _MD_BOLD_U.sub(lambda m: f"{_BOLD_TOKEN}{m.group(1)}{_BOLD_TOKEN}", text)
    text = _MD_ITALIC.sub(lambda m: f"_{m.group(1)}_", text)
    text = text.replace(_BOLD_TOKEN, "*")
    text = _MD_HEADING.sub(lambda m: f"*{m.group(1)}*", text)
    text = _MD_STRIKE.sub(lambda m: f"~{m.group(1)}~", text)
    text = _MD_LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    return text


def markdown_to_mrkdwn(text: str | None) -> str:
    """Markdown as agents write it -> mrkdwn as Slack renders it.

    Deliberately narrow: bold, italic, headings, strikethrough, links — the
    constructs MC's comment format actually uses. Anything exotic passes
    through unchanged; wrongly transforming is worse than not transforming.
    """
    if not text:
        return ""
    parts = _CODE_SPLIT.split(text)
    # Odd indices are the captured code segments — untouchable.
    return "".join(
        part if i % 2 else _prose_to_mrkdwn(part) for i, part in enumerate(parts)
    )


# ── Adapter ───────────────────────────────────────────────────────────────


def _truncate(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _TITLE_MAX else text[: _TITLE_MAX - 1] + "…"


async def _thread_headline(session: AsyncSession, thread: Thread) -> str:
    """The parent message that opens a thread's Slack thread.

    Same convention as the Telegram topic title (`#<short-id> <task title>`) —
    the operator should recognise the same conversation in both channels.
    """
    if thread.task_id is not None:
        task = (
            await session.exec(select(Task).where(Task.id == thread.task_id))
        ).one_or_none()
        if task is not None:
            return _truncate(f"#{str(task.id)[:8]} {task.title}")
    if thread.project_id is not None:
        project = (
            await session.exec(select(Project).where(Project.id == thread.project_id))
        ).one_or_none()
        if project is not None:
            return _truncate(project.name)
    if thread.title:
        return _truncate(thread.title)
    return _truncate(f"Thread {str(thread.id)[:8]}")


class SlackChatAdapter(BaseChatAdapter):
    key = "slack"
    label = "Slack"
    # The difference to Telegram, and the reason this channel exists.
    capabilities = ChatCapabilities(sender_identity=True, rooms=True)

    def __init__(self, *, transport=None, faces=None):
        """Transport and face lookup are injectable so tests never touch the
        network or the production engine; omitted, the real ones are used."""
        self._transport_override = transport
        self._faces = faces if faces is not None else SlackFaces()

    # ── Switch ───────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        from app.config import settings

        return bool(getattr(settings, "slack_team_chat_enabled", False))

    def is_configured(self) -> bool:
        """Target channel set AND a bot token in the vault.

        Kept separate from ``is_enabled`` on purpose (ADR-072): the room
        lifecycle runs on the switch alone, sending additionally needs
        credentials. Merging them would silently change one of the two.
        """
        from app.services import slack_client

        return bool(self._channel()) and slack_client.bot_token_looks_present()

    # ── Transport ────────────────────────────────────────────────────────

    def _channel(self) -> str:
        from app.config import settings

        return (getattr(settings, "slack_default_channel", "") or "").strip()

    def _transport(self):
        if self._transport_override is not None:
            return self._transport_override
        from app.services.slack_client import SlackTransport

        return SlackTransport()

    # ── Rooms ────────────────────────────────────────────────────────────

    async def ensure_room(
        self, session: AsyncSession, thread: Thread
    ) -> ChatRoomRef | None:
        """The Slack thread this MC thread talks in, opened on first use.

        Idempotent. Returns ``GENERAL_ROOM`` for the general chat (it speaks in
        the channel, never in a thread) and None when Slack is not ready — the
        message is then skipped, not lost, and the next one retries.
        """
        if thread.slack_thread_ts:
            return thread.slack_thread_ts
        if thread.kind == "dm":
            return GENERAL_ROOM

        channel = self._channel()
        if not channel:
            logger.info(
                "SLACK_DEFAULT_CHANNEL is empty — thread %s stays unmapped", thread.id
            )
            return None

        headline = await _thread_headline(session, thread)
        result = await self._transport().post_message(channel=channel, text=headline)
        if not result.ok or not result.ts:
            logger.warning(
                "slack: could not open a thread for %s — %s", thread.id, result.error
            )
            return None

        thread.slack_thread_ts = result.ts
        session.add(thread)
        try:
            await session.commit()
        except IntegrityError:
            # Two concurrent calls can each open a Slack thread for the same MC
            # thread. Same belt-and-braces as Telegram (uq_threads_slack_thread_ts):
            # take the persisted winner rather than blowing up; if there is none,
            # degrade like "not ready".
            await session.rollback()
            winner = (
                await session.exec(select(Thread).where(Thread.id == thread.id))
            ).one_or_none()
            return winner.slack_thread_ts if winner is not None else None
        return result.ts

    async def resolve_thread_for_room(self, session: AsyncSession, room: ChatRoomRef):
        """Inbound direction. An unknown ts resolves to None — the neutral path
        asks back instead of guessing. ``GENERAL_ROOM`` is not a thread, so it
        resolves to None as well."""
        if not room:
            return None
        return (
            await session.exec(
                select(Thread).where(Thread.slack_thread_ts == str(room))
            )
        ).one_or_none()

    async def handle_task_done(self, session: AsyncSession, task) -> None:
        """No room bookkeeping on Slack.

        Telegram renames its topic to "✓ …" because a forum topic list is the
        only place a finished task is visible there. A Slack thread carries the
        completion message itself (the neutral pipeline mirrors it), and Slack
        has no equivalent of closing a thread. Deliberately a no-op, not a gap.
        """
        return None

    async def purge_rooms(self, older_than_days: int) -> int:
        """Nothing to purge: deleting a Slack thread would delete the operator's
        conversation history, which is the opposite of what the 30-day topic
        purge does on Telegram (where topics, not messages, pile up)."""
        return 0

    # ── Messages ─────────────────────────────────────────────────────────

    async def send(
        self, room: ChatRoomRef | None, message: OutboundChatMessage
    ) -> bool:
        channel = self._channel()
        if not channel:
            logger.warning("slack send skipped: SLACK_DEFAULT_CHANNEL is empty")
            return False

        username: str | None = None
        icon: str | None = None
        if message.sender is not None:
            # Identity is carried, not degraded: Rex posts as Rex, with Rex's
            # face. `sender is None` means the MC app speaks as itself.
            username = message.sender.display_name
            icon = await self._faces.face_for(message.sender)

        try:
            result = await self._transport().post_message(
                channel=channel,
                text=markdown_to_mrkdwn(message.body),
                username=username,
                icon_emoji=icon,
                thread_ts=(str(room) if room else None),
                silent=message.silent,
            )
        except Exception as e:  # noqa: BLE001 — a send error must never raise
            logger.warning("slack send failed: %s", e)
            return False

        if not result.ok:
            logger.warning("slack send failed: %s", result.error)
        return result.ok
