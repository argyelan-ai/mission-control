"""Eingehend: Telegram-Nachricht -> MC-Thread (P2.4).

Marks Nachricht aus einem Telegram-Thema wird als sender_type="user" im
zugehoerigen Thread abgelegt; die bestehende Nudge+Pull-Kette stellt sie den
Beteiligten zu. Die Thema-Nummer (`message_thread_id`) bestimmt den Ziel-Thread —
kein Raten, kein `@`.

Routing:
  * `message_thread_id` gesetzt -> Thread via `threads.telegram_topic_id`.
      - gefunden   -> post_message in diesen Thread.
      - unbekannt  -> NICHT raten (Mark legte das Thema evtl. von Hand an):
                      im selben Thema zurueckfragen, zu welcher Aufgabe es gehoert.
  * kein `message_thread_id` (Allgemein-Thema) -> DM-Thread mit Boss.
  * Sprachnachricht -> STT (`jarvis_stt_model`, via injiziertem Transkriber) ->
                       Text -> identischer Weg.

Schleifenschutz: der Ingest ruft `post_message(..., mirror_to_telegram=False)`.
Sonst spiegelte der ausgehende Pfad (P2.3) die eingehende Nachricht sofort wieder
nach Telegram — Endlosschleife. Siehe telegram_outbound-Modulkopf (Sperre 2).

Sicherheit: hartes chat_id-Gate — nur Nachrichten aus `settings.telegram_chat_id`
werden verarbeitet, alles andere wird geloggt und verworfen (nie an Fremde
antworten). Ausfallsicher: Fehler pro Nachricht bleiben lokal (der Aufrufer
isoliert sie zusaetzlich), damit der Poll-Loop nie stirbt.

Kein Netz in Tests: Bot und Transkriber werden injiziert/gefaelscht.
"""
from __future__ import annotations

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.board import Project
from app.models.task import Task
from app.models.thread import Thread
from app.services.messaging import ensure_dm_thread, post_message

logger = logging.getLogger("mc.telegram_inbound")

# Der Allgemein-Chat ist der DM-Thread Mark <-> Boss. Boss traegt den stabilen
# Slug "boss" (Fleet-Konvention, vgl. agent_lifecycle._SINGLETON_BRIDGE_SLUGS).
BOSS_SLUG = "boss"

_UNKNOWN_TOPIC_REPLY = (
    "Zu welcher Aufgabe gehört dieses Thema? Ich kann es keiner laufenden Aufgabe "
    "zuordnen — sag mir kurz die Aufgabe, dann verknüpfe ich es."
)
_NO_BOSS_REPLY = (
    "Ich kann den Allgemein-Chat gerade niemandem zuordnen (kein Boss-Agent gefunden)."
)
_VOICE_FAILED_REPLY = (
    "Ich konnte die Sprachnachricht nicht verstehen — versuch's nochmal oder tipp's kurz."
)
_UNSUPPORTED_MEDIA_REPLY = (
    "Ich kann aktuell nur Text- und Sprachnachrichten verarbeiten."
)


async def ingest_inbound_message(
    session: AsyncSession,
    message: dict,
    *,
    bot,
    transcribe=None,
) -> None:
    """Verarbeite EIN eingehendes Telegram ``message``-Update (Text oder Voice).

    ``bot`` liefert ``send_message(text, message_thread_id=...)`` und
    ``get_file_bytes(file_id)``. ``transcribe`` ist ein optionaler async-Callable
    ``(audio_bytes) -> str | None`` fuer Sprachnachrichten (in Produktion an die
    geteilte STT-Kette gebunden); fehlt er, wird eine Voice-Nachricht sauber
    abgelehnt statt still verworfen.
    """
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not chat_id or chat_id != str(settings.telegram_chat_id):
        from_user = (message.get("from") or {}).get("username", "unknown")
        logger.warning(
            "inbound from unauthorized chat %s (user=%s) — ignored",
            chat_id or "?", from_user,
        )
        return  # NIE an Fremde antworten

    # message_thread_id ist die Telegram-Thema-Nummer; fehlt sie, ist es das
    # Allgemein-Thema (Chat-Stamm).
    topic_id = message.get("message_thread_id")

    text = (message.get("text") or "").strip()
    if not text and message.get("voice"):
        text = await _transcribe_voice(message["voice"], bot=bot, transcribe=transcribe)
        if not text:
            await _reply(bot, topic_id, _VOICE_FAILED_REPLY)
            return
    if not text:
        # Autorisierter Chat, aber weder Text noch (verarbeitbare) Voice.
        logger.info("inbound: unsupported media type from operator chat")
        await _reply(bot, topic_id, _UNSUPPORTED_MEDIA_REPLY)
        return

    if topic_id is None:
        thread = await _general_chat_thread(session)
        if thread is None:
            logger.warning("Allgemein-Chat: Boss-Agent nicht gefunden — Nachricht verworfen")
            await _reply(bot, None, _NO_BOSS_REPLY)
            return
    else:
        thread = await _thread_for_topic(session, topic_id)
        if thread is None:
            # Mark legte das Thema evtl. von Hand an — nicht raten, nachfragen.
            logger.info("inbound: unbekanntes Thema %s — Rueckfrage statt Raten", topic_id)
            await _reply(bot, topic_id, _UNKNOWN_TOPIC_REPLY)
            return

    await post_message(
        session,
        thread_id=thread.id,
        sender_type="user",
        message_type="message",
        body=text,
        mirror_to_telegram=False,  # Schleifenschutz (Sperre 2) — nie zurueckspiegeln
    )
    logger.info("inbound Telegram -> thread %s (topic=%s)", thread.id, topic_id)


# ── Helfer ────────────────────────────────────────────────────────────────


async def _reply(bot, topic_id, text: str) -> None:
    """Antworte im selben Thema (bzw. im Chat-Stamm, wenn kein Thema)."""
    try:
        await bot.send_message(text, message_thread_id=topic_id)
    except Exception as e:  # noqa: BLE001 — eine fehlgeschlagene Antwort darf nie werfen
        logger.warning("inbound reply failed: %s", e)


async def _transcribe_voice(voice: dict, *, bot, transcribe) -> str | None:
    """Laedt die Sprachnotiz und transkribiert sie via injiziertem Transkriber.

    Gibt None zurueck, wenn kein Transkriber verdrahtet ist (z.B. jarvis_core
    nicht gemountet), der Download scheitert oder die Transkription wirft — der
    Aufrufer degradiert dann sauber.
    """
    if transcribe is None:
        logger.warning("inbound voice, aber kein Transkriber verdrahtet — abgelehnt")
        return None
    file_id = voice.get("file_id")
    if not file_id:
        return None
    audio = await bot.get_file_bytes(file_id)
    if not audio:
        logger.warning("inbound voice: Datei-Download fehlgeschlagen (%s)", file_id)
        return None
    try:
        transcript = await transcribe(audio)
    except Exception as e:  # noqa: BLE001 — STT-Fehler darf den Loop nie kippen
        logger.warning("inbound voice transcription failed: %s", e)
        return None
    return (transcript or "").strip() or None


async def _thread_for_topic(session: AsyncSession, topic_id: int) -> Thread | None:
    """Welcher MC-Thread gehoert zu diesem Telegram-Thema?

    Zwei Besitzer-Arten (P3.1): ein Ad-hoc-Task-Thema haengt am Thread selbst,
    ein PROJEKT-Thema am Projekt (dort reden alle seine Tasks — threads.
    telegram_topic_id ist unique, mehrere Task-Threads koennten sich die ID also
    gar nicht teilen). Faellt die Projekt-Aufloesung weg, beantwortet MC jede
    Nachricht in einem Projekt-Thema mit „unbekanntes Thema" — der Rueckkanal
    waere tot.

    Bei einem Projekt-Thema landet die Nachricht im juengsten OFFENEN Task-Thread
    des Projekts (das ist die Aufgabe, an der gerade gearbeitet wird); gibt es
    keinen offenen, im juengsten ueberhaupt.
    """
    thread = (
        await session.exec(select(Thread).where(Thread.telegram_topic_id == topic_id))
    ).one_or_none()
    if thread is not None:
        return thread

    project = (
        await session.exec(select(Project).where(Project.telegram_topic_id == topic_id))
    ).one_or_none()
    if project is None:
        return None

    candidates = (
        await session.exec(
            select(Thread)
            .join(Task, Task.thread_id == Thread.id, isouter=True)
            .where((Thread.project_id == project.id) | (Task.project_id == project.id))
            .order_by(Thread.created_at.desc())
        )
    ).all()
    if not candidates:
        return None
    open_threads = [t for t in candidates if t.closed_at is None]
    return (open_threads or candidates)[0]


async def _general_chat_thread(session: AsyncSession) -> Thread | None:
    """Der Allgemein-Chat = DM-Thread mit Boss. None, wenn kein Boss existiert."""
    boss = (
        await session.exec(select(Agent).where(Agent.slug == BOSS_SLUG))
    ).first()
    if boss is None:
        return None
    return await ensure_dm_thread(session, boss)
