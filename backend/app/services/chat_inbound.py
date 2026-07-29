"""Channel-neutral inbound routing: Chat-Nachricht -> MC-Thread (ADR-072).

The channel keeps what only it can do — authenticate the sender's chat, parse
its payload (text, voice, attachments), send its own replies. What every
channel shares is the routing decision, and it lives here:

  * message came from a room     -> ``adapter.resolve_thread_for_room``
      - known room               -> that thread
      - unknown room             -> DON'T GUESS, ask back (the operator may
                                    have created the room by hand)
  * message came without a room  -> the general chat = DM thread with Boss
      - no Boss agent            -> say so, don't drop it silently

Plus the loop-protection contract for the write: an inbound message is stored
as ``sender_type="user"`` with the outbound mirror suppressed, otherwise the
outbound pipeline would bounce it straight back into the channel. The channel
performs the write itself (its own session/monkeypatch surface), but it must
use ``INBOUND_MESSAGE_KWARGS`` so the rule exists exactly once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.thread import Thread
from app.services.chat_adapter import ChatAdapter, ChatRoomRef

logger = logging.getLogger("mc.chat_inbound")

# Der Allgemein-Chat ist der DM-Thread Mark <-> Boss. Boss traegt den stabilen
# Slug "boss" (Fleet-Konvention, vgl. agent_lifecycle._SINGLETON_BRIDGE_SLUGS).
BOSS_SLUG = "boss"

UNKNOWN_ROOM_REPLY = (
    "Zu welcher Aufgabe gehört dieses Thema? Ich kann es keiner laufenden Aufgabe "
    "zuordnen — sag mir kurz die Aufgabe, dann verknüpfe ich es."
)
NO_BOSS_REPLY = (
    "Ich kann den Allgemein-Chat gerade niemandem zuordnen (kein Boss-Agent gefunden)."
)

# Wie eine eingehende Nachricht geschrieben wird — Schleifenschutz inklusive.
# Ein Kanal darf diese Regel nicht selbst formulieren; er reicht sie durch.
INBOUND_MESSAGE_KWARGS = {
    "sender_type": "user",
    "message_type": "message",
    "mirror_to_telegram": False,  # historischer Parametername, kanal-neutral gemeint
}


@dataclass(frozen=True)
class InboundRoute:
    """Wohin gehoert diese eingehende Nachricht?

    Genau eines ist gesetzt: ``thread`` (dorthin schreiben) oder ``notice``
    (dem Operator im selben Raum antworten, nichts schreiben).
    """

    thread: Thread | None = None
    notice: str | None = None


async def route_inbound(
    session: AsyncSession, adapter: ChatAdapter, room: ChatRoomRef | None
) -> InboundRoute:
    """Die Routing-Entscheidung — kanal-neutral, ohne Seiteneffekte."""
    if room is None:
        thread = await general_chat_thread(session)
        if thread is None:
            logger.warning("Allgemein-Chat: Boss-Agent nicht gefunden — Nachricht verworfen")
            return InboundRoute(notice=NO_BOSS_REPLY)
        return InboundRoute(thread=thread)

    thread = await adapter.resolve_thread_for_room(session, room)
    if thread is None:
        logger.info("inbound: unbekannter Raum %s — Rueckfrage statt Raten", room)
        return InboundRoute(notice=UNKNOWN_ROOM_REPLY)
    return InboundRoute(thread=thread)


async def general_chat_thread(session: AsyncSession) -> Thread | None:
    """Der Allgemein-Chat = DM-Thread mit Boss. None, wenn kein Boss existiert."""
    from app.services.messaging import ensure_dm_thread

    boss = (await session.exec(select(Agent).where(Agent.slug == BOSS_SLUG))).first()
    if boss is None:
        return None
    return await ensure_dm_thread(session, boss)
