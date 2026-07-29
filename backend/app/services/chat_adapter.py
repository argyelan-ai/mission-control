"""ChatAdapter — the contract every team-chat channel implements (ADR-072).

MC's team chat (Interaktionsmodell 2.0) was born on Telegram, and its rules —
what gets mirrored, who is loud, when the operator sleeps, which room a thread
talks in — grew up *inside* the Telegram modules. A second channel (Slack)
would have had to copy them. This module is the seam: the channel-neutral
pipeline lives in ``chat_outbound`` / ``chat_inbound`` / ``chat_rooms`` and
talks to a channel ONLY through the ``ChatAdapter`` protocol below.

Same shape as the host-harness registry (ADR-064,
``services/host_harness_adapter.py``): a ``Protocol`` + a dict registry +
lookup/catalog helpers. Deliberately the same mechanics, not a second one.

── Absender-Identität ist ein eigenes Konzept ────────────────────────────
Telegram sends everything from ONE bot, so an agent's name could only ever be
a text prefix (``"Rex: fertig"``). Slack can set username + icon per message.
If the contract carried the prefix, Slack would inherit Telegram's limitation
as a formatting habit. So the neutral pipeline hands the adapter a
``ChatSender`` (kind + display name + agent id) and the adapter decides how to
render it. A channel that cannot carry identity *degrades it visibly*
(``capabilities.sender_identity is False`` → prefix) instead of dropping it —
that law is asserted for every adapter by the TCK
(``tests/test_chat_adapter_tck.py``).

── Kanal-Schalter ────────────────────────────────────────────────────────
``settings.chat_channels`` (comma list, e.g. ``"slack"`` or
``"telegram,slack"``) selects which registered adapters may run; empty (the
default) means "no explicit selection — every registered adapter may run".
On top of that each adapter reports:

  * ``is_enabled()``    — the operator switched this channel ON
                          (Telegram: ``telegram_team_chat_enabled``),
  * ``is_configured()`` — credentials/room targets are present.

Two flags, not one, because the existing Telegram code already gated two
different things differently: the room lifecycle (✓-rename, purge) ran on the
feature flag alone, while the outbound mirror additionally required bot token +
chat id. ``enabled_chat_adapters()`` and ``sendable_chat_adapters()`` preserve
exactly that distinction.

Nothing selected / nothing enabled is a first-class, silent state: the
selectors return ``[]`` and every neutral entry point becomes a no-op — no
exception, no error log.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger("mc.chat_adapter")

# A room is whatever the channel calls a per-thread conversation space: a
# Telegram forum topic id (int), a Slack channel id (str). MC never interprets
# it — it stores it and hands it back. ``None`` from ``ensure_room`` means "the
# channel is not ready" (degrade, don't raise).
ChatRoomRef = int | str


@dataclass(frozen=True)
class ChatSender:
    """WHO is speaking — first-class, never a formatting detail.

    ``kind`` is the MC message's sender_type ("agent" | "system" | "user").
    ``display_name`` is what a human should see. ``agent_id`` lets a channel
    that can do better than a name (Slack icon/profile per agent) look one up.
    """

    kind: str
    display_name: str
    agent_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ChatCapabilities:
    """What this channel can do. Only capabilities a contract operation
    actually branches on live here — no catalogue on spec.

    sender_identity — can a single message carry its own sender name/avatar?
                      False → the adapter must degrade it visibly (prefix).
    rooms           — does the channel have per-thread rooms? False → the
                      neutral pipeline skips ``ensure_room`` and sends
                      everything to the channel's default room.
    """

    sender_identity: bool
    rooms: bool


@dataclass(frozen=True)
class OutboundChatMessage:
    """One message on its way out.

    ``sender is None`` means the channel bot speaks as itself (a notice such as
    "which task does this topic belong to?") — no identity to render.
    ``silent`` is the ping decision the neutral pipeline already made
    (night quiet hours + loudness rule); the adapter only executes it.
    """

    body: str
    sender: ChatSender | None = None
    silent: bool = False


@runtime_checkable
class ChatAdapter(Protocol):
    """One team-chat channel. Everything channel-specific — and nothing else.

    Failure policy for the whole contract: a chat channel must never break
    agent work. Every method degrades (returns False/None/0 and logs) instead
    of raising; the TCK asserts this per adapter.
    """

    key: str            # "telegram" | "slack" — the value in CHAT_CHANNELS
    label: str          # display name for settings UI / diagnostics
    capabilities: ChatCapabilities

    def is_enabled(self) -> bool:
        """Operator switched this channel on (feature flag)."""
        ...

    def is_configured(self) -> bool:
        """Credentials + target chat present, so sending can actually work."""
        ...

    # ── Rooms ────────────────────────────────────────────────────────────

    async def ensure_room(self, session: AsyncSession, thread) -> ChatRoomRef | None:
        """The room this MC thread talks in, created on first use.

        Idempotent. Returns None when the channel is not ready (not a forum
        yet, rate-limited, transport error) — the message is then skipped, not
        lost, and a later call retries.
        """
        ...

    async def resolve_thread_for_room(self, session: AsyncSession, room: ChatRoomRef):
        """Inbound direction: which MC thread does this room belong to?
        None when unknown — the neutral inbound path then asks back instead of
        guessing."""
        ...

    async def handle_task_done(self, session: AsyncSession, task) -> None:
        """A task reached ``done`` — do the channel's room bookkeeping
        (Telegram: rename the topic to ``✓ …`` and close the thread). Never
        raises: a chat hiccup must not fail a task completion."""
        ...

    async def purge_rooms(self, older_than_days: int) -> int:
        """Periodic cleanup of long-finished rooms. Returns how many were
        removed; swallows its own errors (the daily job must never die)."""
        ...

    # ── Messages ─────────────────────────────────────────────────────────

    async def send(self, room: ChatRoomRef | None, message: OutboundChatMessage) -> bool:
        """Deliver one message into ``room`` (None = the channel's default
        room). Returns True when a send was attempted successfully. Never
        raises."""
        ...

    async def mirror_message(self, session: AsyncSession, message, *, now=None) -> bool:
        """Channel entry for the outbound pipeline. The default implementation
        (``BaseChatAdapter``) simply runs the neutral pipeline against this
        adapter; an adapter overrides it only to wire its own transport
        objects in (see ``chat_telegram.TelegramChatAdapter``)."""
        ...


class BaseChatAdapter:
    """Shared, channel-neutral half of an adapter. Subclass it.

    ``mirror_message`` is deliberately implemented here so a new channel gets
    skip rules, night quiet hours, the loudness rule and sender-identity
    resolution for free — implementing ``send``/``ensure_room`` is enough.
    """

    key: str = ""
    label: str = ""
    capabilities: ChatCapabilities = ChatCapabilities(sender_identity=False, rooms=False)

    def is_enabled(self) -> bool:  # pragma: no cover — overridden
        return False

    def is_configured(self) -> bool:  # pragma: no cover — overridden
        return False

    async def ensure_room(self, session: AsyncSession, thread) -> ChatRoomRef | None:
        return None

    async def resolve_thread_for_room(self, session: AsyncSession, room: ChatRoomRef):
        return None

    async def handle_task_done(self, session: AsyncSession, task) -> None:
        return None

    async def purge_rooms(self, older_than_days: int) -> int:
        return 0

    async def send(  # pragma: no cover — overridden
        self, room: ChatRoomRef | None, message: OutboundChatMessage
    ) -> bool:
        return False

    async def mirror_message(self, session: AsyncSession, message, *, now=None) -> bool:
        from app.services import chat_outbound

        return await chat_outbound.mirror_message(session, message, self, now=now)


# ── Registry ──────────────────────────────────────────────────────────────
#
# INVARIANT (asserted by tests/test_chat_adapter_tck.py): every adapter here
# has a test harness in tests/chat_harnesses.py, so registering a channel
# subscribes it to the whole conformance suite. Adding a channel must be the
# only edit needed — no neutral module changes.

def _registry() -> dict[str, ChatAdapter]:
    """Built lazily: the adapters import the channel modules, which import
    settings and transport clients."""
    global _ADAPTERS
    if _ADAPTERS is None:
        from app.services.chat_slack import SlackChatAdapter
        from app.services.chat_telegram import TelegramChatAdapter

        _ADAPTERS = {
            "telegram": TelegramChatAdapter(),
            "slack": SlackChatAdapter(),
        }
    return _ADAPTERS


_ADAPTERS: dict[str, ChatAdapter] | None = None


def all_chat_adapters() -> list[ChatAdapter]:
    """Every registered adapter, regardless of switch/flags."""
    return list(_registry().values())


def get_chat_adapter(key: str | None) -> ChatAdapter | None:
    if not key:
        return None
    return _registry().get(key)


def selected_channel_keys() -> set[str] | None:
    """Keys from ``settings.chat_channels``; None when nothing is configured
    (= no explicit selection, every registered channel may run).

    Unknown keys are logged once per call and ignored — a typo must not take
    the whole chat down, and it must not silently look like success either.
    """
    from app.config import settings

    raw = (getattr(settings, "chat_channels", "") or "").strip()
    if not raw:
        return None
    keys = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = keys - set(_registry())
    if unknown:
        logger.warning(
            "CHAT_CHANNELS names unknown channel(s) %s — known: %s",
            ", ".join(sorted(unknown)), ", ".join(sorted(_registry())),
        )
    return keys


def enabled_chat_adapters() -> list[ChatAdapter]:
    """Adapters that are selected AND switched on. Used for room lifecycle
    (task-done bookkeeping, purge) — those paths need no credentials of their
    own; each adapter degrades internally when its transport is unavailable."""
    selected = selected_channel_keys()
    out = []
    for key, adapter in _registry().items():
        if selected is not None and key not in selected:
            continue
        try:
            if adapter.is_enabled():
                out.append(adapter)
        except Exception as e:  # noqa: BLE001 — a broken flag must not break chat
            logger.warning("chat adapter %s: is_enabled() failed: %s", key, e)
    return out


def sendable_chat_adapters() -> list[ChatAdapter]:
    """Adapters that are selected, switched on AND configured. Used by the
    outbound mirror — sending without credentials would be a guaranteed
    failure per message."""
    out = []
    for adapter in enabled_chat_adapters():
        try:
            if adapter.is_configured():
                out.append(adapter)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "chat adapter %s: is_configured() failed: %s",
                getattr(adapter, "key", "?"), e,
            )
    return out


def chat_channel_catalog() -> list[dict[str, Any]]:
    """The registry rendered for settings UI / diagnostics — the registry
    answers, the UI asks (same principle as ``host_harness_catalog``)."""
    selected = selected_channel_keys()
    return [
        {
            "key": adapter.key,
            "label": adapter.label,
            "selected": selected is None or adapter.key in selected,
            "enabled": _safe_flag(adapter, "is_enabled"),
            "configured": _safe_flag(adapter, "is_configured"),
            "capabilities": {
                "sender_identity": adapter.capabilities.sender_identity,
                "rooms": adapter.capabilities.rooms,
            },
        }
        for adapter in all_chat_adapters()
    ]


def _safe_flag(adapter: ChatAdapter, name: str) -> bool:
    try:
        return bool(getattr(adapter, name)())
    except Exception:  # noqa: BLE001
        return False
