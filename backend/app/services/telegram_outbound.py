"""Ausgehende Spiegelung: Thread-Nachricht -> Telegram-Thema (P2.3).

Wird eine Nachricht in einen MC-Thread geschrieben, erscheint sie im
zugehoerigen Telegram-Thema — mit Absendername davor (`Rex: …`, `System: …`)
und, je nach Ping-Regel, stumm oder mit Ton. Der Pfad ist ausfallsicher: ein
Telegram-Fehler wird geloggt, nie geworfen — er darf `post_message` und damit
Agentenarbeit nie blockieren. Tests fahren ohne Netz: Topic-Client und Bot
werden injiziert.

── Schleifenschutz (P2.4 baut darauf) ──────────────────────────────────────
Eine aus Telegram *eingehende* Nachricht (P2.4) darf nicht wieder nach Telegram
gespiegelt werden, sonst Endlosschleife. Es gibt ZWEI unabhaengige Sperren:

  1. `sender_type == "user"` wird nie gespiegelt. Mark ist die einzige
     Nutzerquelle; er hat die Nachricht selbst geschrieben (im Web oder aus
     Telegram). Das allein bricht die Schleife fuer den heutigen Inbound-Pfad,
     der eingehende Telegram-Nachrichten als sender_type="user" ablegt.
  2. Expliziter Herkunfts-Schalter `post_message(..., mirror_to_telegram=False)`.
     P2.4 setzt ihn beim Ingest aus Telegram — der dokumentierte Diskriminator,
     der auch dann schuetzt, wenn ein kuenftiger Inbound-Pfad einen anderen
     sender_type verwenden sollte.

Guertel und Hosentraeger: (1) ist semantisch, (2) ist explizit. Zusammen
garantieren sie, dass nichts, was aus Telegram kam, nach Telegram zurueckläuft.
"""
import logging
from datetime import datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.thread import Message, Thread
from app.services.dispatch_delivery import _BRIEFING_MARKER_PREFIX
from app.services.messaging import BACKFILL_SEED_BODY
from app.services.telegram_topics import (
    GENERAL_TOPIC_ID,
    ForumTopicClient,
    ensure_topic_for_thread,
)

logger = logging.getLogger("mc.telegram_outbound")

# Nachtruhe: 23:00–06:59 Ortszeit kein Ton (ausser question priority=critical).
# Der Mac Mini laeuft in CH-Ortszeit — datetime.now() ist damit Ortszeit.
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 7

# question_meta["category"]-Werte, deren Nachricht laut zugestellt wird. Approval
# und Review tauchen erst als Thread-Nachricht auf, wenn ihre in-Thread-Wiring
# gebaut ist (nicht Teil von P2.3) — dies ist der dokumentierte Slot, an dem
# ihre Lautstaerke haengt.
_LOUD_CATEGORIES = ("approval", "review")


def _is_night(now: datetime) -> bool:
    return now.hour >= NIGHT_START_HOUR or now.hour < NIGHT_END_HOUR


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
    if message.sender_type == "user":
        return "user-message"          # Mark schrieb sie (Schleifenschutz, s.o.)
    body = message.body or ""
    if _BRIEFING_MARKER_PREFIX in body:
        return "dispatch-briefing"     # internes 8k-Briefing, gehoert nicht in Chat
    if body == BACKFILL_SEED_BODY:
        return "backfill-seed"         # Migrations-Artefakt
    return None


def _sender_prefix(message: Message, sender_name: str | None) -> str:
    if message.sender_type == "system":
        return "System"
    return sender_name or "Agent"


async def mirror_message_to_telegram(
    session: AsyncSession,
    message: Message,
    *,
    topic_client: ForumTopicClient,
    bot,
    now: datetime | None = None,
) -> bool:
    """Spiegle eine Thread-Message in ihr Telegram-Thema.

    Gibt True zurueck, wenn ein Sendeversuch lief, sonst False (uebersprungen
    oder Telegram nicht bereit). Wirft NIE — jeder Fehler wird geloggt, damit der
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

        topic_id = await ensure_topic_for_thread(session, thread, topic_client)
        if topic_id is None:
            logger.info(
                "Telegram nicht bereit (Thread %s ungemappt) — msg %s nicht gespiegelt",
                thread.id, message.id,
            )
            return False

        sender_name = None
        if message.sender_type == "agent" and message.sender_id is not None:
            agent = (
                await session.exec(select(Agent).where(Agent.id == message.sender_id))
            ).one_or_none()
            sender_name = agent.name if agent is not None else None

        text = f"{_sender_prefix(message, sender_name)}: {message.body}"
        disable = _should_disable_notification(message, now or datetime.now())
        # GENERAL_TOPIC_ID (0) -> ohne message_thread_id (Chat-Stamm). send_message
        # laesst den falsy Wert ohnehin weg; wir sind hier explizit.
        thread_arg = None if topic_id == GENERAL_TOPIC_ID else topic_id

        await bot.send_message(
            text,
            message_thread_id=thread_arg,
            disable_notification=disable,
        )
        return True
    except Exception as e:  # noqa: BLE001 — der Spiegel darf post_message nie kippen
        logger.warning("mirror_message_to_telegram fehlgeschlagen: %s", e)
        return False
