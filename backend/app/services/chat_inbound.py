"""Channel-neutral inbound routing: Chat-Nachricht -> MC-Thread (ADR-072).

The channel keeps what only it can do — authenticate the sender's chat, parse
its payload (text, voice, attachments), send its own replies. What every
channel shares is the routing decision, and it lives here:

  * message came from a room     -> ``adapter.resolve_thread_for_room``
      - known room               -> that thread
      - unknown room             -> DON'T GUESS, ask back (the operator may
                                    have created the room by hand)
  * message came without a room  -> the general chat = DM thread with Boss
      - unless the text names an agent (``@rex``) -> that agent's DM thread
      - no Boss agent            -> say so, don't drop it silently

── Wer ist gemeint ───────────────────────────────────────────────────────
Every route above ends in exactly ONE thread, and that is the whole
addressing rule: a message is delivered to one conversation, never fanned out
to the fleet. Without it a plain "hallo" in the channel would reach ten agents
and produce ten answers.

``@name`` is parsed out of the TEXT (``resolve_addressed_agent``), not read
from a channel's mention payload. The agents are not users of any chat
channel — MC's bot merely speaks under their names — so there is no real
mention to read and no autocomplete to rely on. Matching is therefore
deliberately tolerant (case, ``@`` or not, ``-`` vs ``_``) and, without a
leading ``@``, restricted to the first word so that "ich habe rex gefragt"
does not silently re-route.

Plus the loop-protection contract for the write: an inbound message is stored
as ``sender_type="user"`` with the outbound mirror suppressed, otherwise the
outbound pipeline would bounce it straight back into the channel. The channel
performs the write itself (its own session/monkeypatch surface), but it must
use ``INBOUND_MESSAGE_KWARGS`` so the rule exists exactly once.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

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

    ``mentions`` traegt die im Text erkannten Adressaten mit — sie wandern in
    ``Message.mentions``, damit im Thread sichtbar bleibt, wer gemeint war.
    ``addressed_agent`` ist der Agent, an dem das Routing tatsaechlich haengt
    (None = niemand namentlich adressiert).
    """

    thread: Thread | None = None
    notice: str | None = None
    mentions: list[str] = field(default_factory=list)
    addressed_agent: Agent | None = None


# ── @name aus dem Text ────────────────────────────────────────────────────
#
# Ein Handle ist alles, was nach einem Agentennamen aussieht. Bewusst breit
# gefasst (Punkte/Striche/Unterstriche erlaubt) und erst beim VERGLEICH
# normalisiert — "@free-code", "@Free_Code" und "FreeCode:" sind derselbe Agent.
_HANDLE_ANYWHERE = re.compile(r"@([A-Za-z][A-Za-z0-9._-]{0,63})")
_LEADING_HANDLE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9._-]{0,63})\s*[:,]?\s")


def _fold(handle: str) -> str:
    """Vergleichsform eines Namens: nur Buchstaben+Ziffern, klein.

    Damit fallen Gross/Kleinschreibung, ``-`` vs ``_`` und ein angehaengtes
    Satzzeichen aus dem Vergleich heraus — genau die Varianten, die ein Mensch
    tippt, wenn ihm keine Autovervollstaendigung hilft.
    """
    return re.sub(r"[^a-z0-9]", "", (handle or "").lower())


def parse_handles(text: str) -> tuple[list[str], list[str]]:
    """(explizite ``@handles``, Kandidaten in Pruefreihenfolge).

    Mit ``@`` zaehlt jedes Vorkommen, egal wo im Satz — das ist eine klare
    Absicht und wird deshalb IMMER als Erwaehnung vermerkt. Ohne ``@`` zaehlt
    nur das erste Wort ("Rex: bitte pruefen"); dieses Wort ist bloss ein
    Kandidat und wird nur dann zur Erwaehnung, wenn wirklich ein Agent so
    heisst — sonst stuende in jeder Nachricht ihr erstes Wort als Erwaehnung.
    """
    explicit = _HANDLE_ANYWHERE.findall(text or "")
    if explicit:
        return explicit, explicit
    leading = _LEADING_HANDLE.match(text or "")
    return [], ([leading.group(1)] if leading else [])


async def resolve_addressed_agent(
    session: AsyncSession, text: str | None
) -> tuple[Agent | None, list[str]]:
    """(gemeinter Agent, Erwaehnungen) — ohne Seiteneffekte.

    Verglichen wird gegen Name UND Slug, und nur ARCHIVIERT-freie Agenten
    zaehlen: ein archivierter Agent laeuft nicht und koennte nicht antworten —
    die Nachricht landet dann bei Boss statt in einem toten DM-Thread. Ein
    ``@handle``, zu dem kein Agent existiert, faellt genauso still durch: er
    bleibt als Erwaehnung stehen (damit die Absicht im Thread sichtbar ist),
    aendert aber das Routing nicht.
    """
    explicit, candidates = parse_handles(text or "")
    if not candidates:
        return None, []

    agents = (
        await session.exec(select(Agent).where(Agent.archived_at.is_(None)))
    ).all()
    by_key: dict[str, Agent] = {}
    for agent in agents:
        for key in (_fold(agent.slug or ""), _fold(agent.name or "")):
            if key:
                by_key.setdefault(key, agent)

    for handle in candidates:
        match = by_key.get(_fold(handle))
        if match is not None:
            mentions = explicit or [handle]
            return match, mentions
    return None, explicit


async def route_inbound(
    session: AsyncSession,
    adapter: ChatAdapter,
    room: ChatRoomRef | None,
    *,
    text: str | None = None,
    anchor: ChatRoomRef | None = None,
) -> InboundRoute:
    """Die Routing-Entscheidung — kanal-neutral, ohne Seiteneffekte.

    ``text`` ist optional: ein Kanal, der ``@name`` unterstuetzt, reicht ihn
    durch. Ohne ``text`` ist das Verhalten Byte fuer Byte das bisherige (so
    ruft Telegram weiterhin auf) — kein Kanal aendert sich, weil ein anderer
    dazukommt.

    ``anchor`` ist der Thread-Anker-Fix (2026-08-05): kann der Kanal die
    Wurzel-Nachricht des Operators nativ zum Gespraechsfaden machen (Slack:
    ihre ``ts``), reicht er sie hier durch. Die Nachricht oeffnet dann ihr
    eigenes Gespraech (``kind="chat"``), an den Anker gebunden — jede Antwort
    erscheint als echte Thread-Antwort UNTER der Nachricht statt als neue
    Kanal-Nachricht aus dem einen Boss-DM-Thread. Ohne ``anchor`` bleibt das
    Verhalten unveraendert (Telegram uebergibt keinen).
    """
    if room is None:
        addressed, handles = await resolve_addressed_agent(session, text)
        target = addressed
        if target is None:
            target = (
                await session.exec(select(Agent).where(Agent.slug == BOSS_SLUG))
            ).first()
            if target is None:
                logger.warning(
                    "Allgemein-Chat: Boss-Agent nicht gefunden — Nachricht verworfen"
                )
                return InboundRoute(notice=NO_BOSS_REPLY, mentions=handles)

        thread = None
        if anchor is not None:
            thread = await _anchored_chat_thread(session, adapter, target, anchor, text)
        if thread is None:
            from app.services.messaging import ensure_dm_thread

            thread = await ensure_dm_thread(session, target)
            logger.info(
                "inbound: %s -> DM-Thread %s",
                f"@{target.name} adressiert" if addressed else "Allgemein-Chat",
                thread.id,
            )
        return InboundRoute(
            thread=thread, mentions=handles, addressed_agent=addressed
        )

    thread = await adapter.resolve_thread_for_room(session, room)
    if thread is None:
        logger.info("inbound: unbekannter Raum %s — Rueckfrage statt Raten", room)
        return InboundRoute(notice=UNKNOWN_ROOM_REPLY)
    # Im Aufgaben-Raum ist der zustaendige Agent gemeint — ohne dass ihn jemand
    # ansprechen muss. Ein ``@name`` wird hier nur noch vermerkt, nicht geroutet:
    # den Faden zu wechseln, weil jemand einen Namen erwaehnt, waere Raten.
    _, handles = await resolve_addressed_agent(session, text)
    return InboundRoute(thread=thread, mentions=handles)


async def _anchored_chat_thread(
    session: AsyncSession,
    adapter: ChatAdapter,
    agent: Agent,
    anchor: ChatRoomRef,
    text: str | None,
) -> Thread | None:
    """Das Gespraech zu diesem Anker — vorhandenes wiederverwendet (Slack
    liefert Events mehrfach), sonst neu angelegt und gebunden.

    None nur, wenn der Kanal den Anker nicht binden kann und auch kein
    Gespraech dazu existiert — der Aufrufer faellt dann auf den DM-Weg
    zurueck, es geht nie eine Nachricht verloren.
    """
    from app.services.messaging import create_chat_thread

    existing = await adapter.resolve_thread_for_room(session, anchor)
    if existing is not None:
        return existing

    title = (text or "").strip().splitlines()[0][:80] if text else None
    thread = await create_chat_thread(session, agent, title=title or None)
    if await adapter.bind_room(session, thread, anchor):
        logger.info(
            "inbound: Wurzel-Nachricht -> Chat-Thread %s (Anker %s, %s)",
            thread.id, anchor, agent.name,
        )
        return thread

    # Bind verloren (Race: derselbe Anker wurde parallel gebunden) — der
    # Gewinner hat das Gespraech, der frische Thread bleibt leer zurueck.
    winner = await adapter.resolve_thread_for_room(session, anchor)
    if winner is not None:
        return winner
    return None


async def general_chat_thread(session: AsyncSession) -> Thread | None:
    """Der Allgemein-Chat = DM-Thread mit Boss. None, wenn kein Boss existiert."""
    from app.services.messaging import ensure_dm_thread

    boss = (await session.exec(select(Agent).where(Agent.slug == BOSS_SLUG))).first()
    if boss is None:
        return None
    return await ensure_dm_thread(session, boss)
