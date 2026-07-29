"""Channel-neutral outbound pipeline: Thread-Nachricht -> Chat-Kanal (ADR-072).

Everything here used to live in ``telegram_outbound`` and is unchanged in
behaviour — only its home moved, so a second channel inherits the rules instead
of copying them:

  * WHAT is mirrored at all (loop protection, internal briefings, migration
    seeds),
  * WHO is speaking (``ChatSender`` — resolved once, rendered by the channel),
  * HOW LOUD it arrives (ping rule + operator night quiet hours),
  * WHICH room it goes to (delegated to the adapter, skipped for roomless
    channels).

The only channel-specific step left is the last one: ``adapter.send``.

Loop protection (unchanged, see ADR/`telegram_outbound` history): a message
that came FROM a chat channel is stored with ``sender_type="user"`` AND with
``post_message(..., mirror_to_telegram=False)``. Belt and braces — the first is
semantic, the second explicit.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.thread import Message, Thread
from app.services.chat_adapter import ChatAdapter, ChatSender, OutboundChatMessage

logger = logging.getLogger("mc.chat_outbound")

# Nachtruhe: 23:00–06:59 Ortszeit des Operators kein Ton (ausser priority=critical).
#
# ⚠️ NICHT datetime.now() verwenden. Der Kommentar hier behauptete frueher, der
# Mac Mini laufe in CH-Zeit — das stimmt fuer den HOST, aber post_message laeuft
# im Backend-CONTAINER, und der steht auf UTC (live geprueft 27.07.: Container
# 19:56 waehrend es in Zuerich 21:56 war). Mit naiver Ortszeit-Annahme waere die
# Nacht-Grenze um 1–2h verschoben: ein lauter Approval-Ping um 08:00 CEST faellt
# in UTC-06:00 und damit unter NIGHT_END_HOUR — er kaeme STUMM an und bliebe
# liegen. Genau der Schaden, den die Regel verhindern soll.
# ZoneInfo erledigt zugleich die Sommerzeit.
OPERATOR_TZ = ZoneInfo("Europe/Zurich")
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 7

# question_meta["category"]-Werte, deren Nachricht laut zugestellt wird. Approval
# und Review tauchen erst als Thread-Nachricht auf, wenn ihre in-Thread-Wiring
# gebaut ist (nicht Teil von P2.3) — dies ist der dokumentierte Slot, an dem
# ihre Lautstaerke haengt.
_LOUD_CATEGORIES = ("approval", "review")


def _is_night(now: datetime) -> bool:
    """Nacht in der Zeitzone des Operators.

    Ein zeitzonen-behaftetes ``now`` wird umgerechnet; ein naives wird als
    bereits lokal betrachtet (so uebergeben es die Tests).
    """
    local = now.astimezone(OPERATOR_TZ) if now.tzinfo is not None else now
    return local.hour >= NIGHT_START_HOUR or local.hour < NIGHT_END_HOUR


def _mentions_mark(mentions) -> bool:
    for m in mentions or []:
        if str(m).lstrip("@").strip().lower() == "mark":
            return True
    return False


def _ping_is_loud(message: Message) -> bool:
    """Die vier Ton-Ausloeser aus dem Ursprungsdesign:
      (a) @Mark in mentions, (b) message_type == "question" (deckt auch
      Approval-Rueckfragen an Mark ab, die als Frage auftreten),
      (c) Approval, (d) Review — via question_meta["category"].
    Alles andere ist stumm.
    """
    if _mentions_mark(message.mentions):
        return True
    if message.message_type == "question":
        return True
    if (message.question_meta or {}).get("category") in _LOUD_CATEGORIES:
        return True
    return False


def _should_disable_notification(message: Message, now: datetime) -> bool:
    """True = stumm senden. Nachtruhe hat Vorrang: 23–07 ist alles stumm, ausser
    einer als `critical` markierten Frage. Tagsueber gilt die Ping-Regel."""
    if _is_night(now):
        critical = (message.question_meta or {}).get("priority") == "critical"
        return not critical
    return not _ping_is_loud(message)


def _skip_reason(message: Message) -> str | None:
    """Grund, diese Nachricht NICHT zu spiegeln — oder None (= spiegeln)."""
    from app.services.dispatch_delivery import _BRIEFING_MARKER_PREFIX
    from app.services.messaging import BACKFILL_SEED_BODY

    if message.sender_type == "user":
        return "user-message"          # Mark schrieb sie (Schleifenschutz, s.o.)
    body = message.body or ""
    if _BRIEFING_MARKER_PREFIX in body:
        return "dispatch-briefing"     # internes 8k-Briefing, gehoert nicht in Chat
    if body == BACKFILL_SEED_BODY:
        return "backfill-seed"         # Migrations-Artefakt
    return None


async def resolve_sender(session: AsyncSession, message: Message) -> ChatSender:
    """Wer spricht? Kanal-neutral aufgeloest, damit jeder Kanal dieselbe
    Identitaet bekommt und selbst entscheidet, wie er sie zeigt (Slack: eigener
    Absendername/Avatar; Telegram: Prefix, weil alles vom selben Bot kommt)."""
    if message.sender_type == "system":
        return ChatSender(kind="system", display_name="System")

    display = None
    if message.sender_type == "agent" and message.sender_id is not None:
        agent = (
            await session.exec(select(Agent).where(Agent.id == message.sender_id))
        ).one_or_none()
        display = agent.name if agent is not None else None
    return ChatSender(
        kind=message.sender_type,
        display_name=display or "Agent",
        agent_id=message.sender_id,
    )


async def mirror_message(
    session: AsyncSession,
    message: Message,
    adapter: ChatAdapter,
    *,
    now: datetime | None = None,
) -> bool:
    """Spiegle eine Thread-Message in den Raum ihres Threads auf ``adapter``.

    Gibt True zurueck, wenn ein Sendeversuch lief, sonst False (uebersprungen
    oder Kanal nicht bereit). Wirft NIE — jeder Fehler wird geloggt, damit der
    Aufrufer (`post_message`) und die Agentenarbeit nie kippen.
    """
    try:
        reason = _skip_reason(message)
        if reason is not None:
            logger.debug("mirror skip (%s) msg=%s", reason, message.id)
            return False

        thread = (
            await session.exec(select(Thread).where(Thread.id == message.thread_id))
        ).one_or_none()
        if thread is None:
            logger.warning("mirror: Thread %s nicht gefunden", message.thread_id)
            return False

        room = None
        if adapter.capabilities.rooms:
            room = await adapter.ensure_room(session, thread)
            if room is None:
                logger.info(
                    "%s nicht bereit (Thread %s ungemappt) — msg %s nicht gespiegelt",
                    adapter.label, thread.id, message.id,
                )
                return False

        sender = await resolve_sender(session, message)
        silent = _should_disable_notification(
            message, now or datetime.now(tz=OPERATOR_TZ)
        )
        return await adapter.send(
            room, OutboundChatMessage(body=message.body, sender=sender, silent=silent)
        )
    except Exception as e:  # noqa: BLE001 — der Spiegel darf post_message nie kippen
        logger.warning("mirror_message (%s) fehlgeschlagen: %s", adapter.key, e)
        return False


async def mirror_message_to_all(
    session: AsyncSession, message: Message, *, now: datetime | None = None
) -> int:
    """Spiegle in JEDEN aktiven Kanal (der Operator darf mehrere gleichzeitig
    fahren). Gibt die Zahl der erfolgten Sendeversuche zurueck. Kein aktiver
    Kanal = 0, still, kein Fehler."""
    from app.services.chat_adapter import sendable_chat_adapters

    sent = 0
    for adapter in sendable_chat_adapters():
        try:
            if await adapter.mirror_message(session, message, now=now):
                sent += 1
        except Exception as e:  # noqa: BLE001 — ein Kanal darf die anderen nicht kippen
            logger.warning("chat mirror via %s failed: %s", adapter.key, e)
    return sent
