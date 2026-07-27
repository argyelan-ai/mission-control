"""Eingehend: Telegram-Nachricht → MC-Thread (P2.4).

Marks Nachricht aus einem Telegram-Thema landet als sender_type="user" im
zugehoerigen Thread; die Thema-Nummer bestimmt den Ziel-Thread (kein Raten).
Ohne Thema (Allgemein-Thema) geht sie in den DM-Thread mit Boss. Schleifenschutz:
der Ingest setzt mirror_to_telegram=False, damit die Nachricht nicht wieder nach
Telegram zurueckläuft.

Kein Netz: Bot + Transkriber werden injiziert/gefaelscht.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.thread import Message, Thread
import app.services.telegram_inbound as ti
from app.services.telegram_inbound import ingest_inbound_message

CHAT_ID = "12345"


@pytest.fixture(autouse=True)
def operator_chat(monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", CHAT_ID, raising=False)


@pytest.fixture
def bot():
    b = AsyncMock()
    b.send_message = AsyncMock(return_value=1)
    b.get_file_bytes = AsyncMock(return_value=b"\x00oggbytes")
    return b


async def _thread_with_topic(session: AsyncSession, topic_id: int) -> Thread:
    t = Thread(kind="task", task_id=None, telegram_topic_id=topic_id)
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


async def _boss(session: AsyncSession) -> Agent:
    agent = Agent(name="Boss", agent_runtime="host", comm_v2=True)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _messages(session: AsyncSession, thread_id: uuid.UUID) -> list[Message]:
    return list((await session.exec(select(Message).where(Message.thread_id == thread_id))).all())


# ── Ziel-Thread via Thema-Nummer ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_in_known_topic_lands_in_its_thread(async_session, bot):
    thread = await _thread_with_topic(async_session, 777)
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "message_thread_id": 777, "text": "mach das anders"},
        bot=bot,
    )
    msgs = await _messages(async_session, thread.id)
    assert [m.body for m in msgs] == ["mach das anders"]
    assert msgs[0].sender_type == "user"


@pytest.mark.asyncio
async def test_ingest_suppresses_the_outbound_mirror(async_session, bot, monkeypatch):
    """Schleifenschutz: der Ingest ruft post_message mit mirror_to_telegram=False,
    sonst spiegelt post_message die eingehende Nachricht sofort zurueck (Endlosschleife)."""
    thread = await _thread_with_topic(async_session, 5)
    captured: dict = {}
    real = ti.post_message

    async def spy(session, **kwargs):
        captured.update(kwargs)
        return await real(session, **kwargs)

    monkeypatch.setattr(ti, "post_message", spy)
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "message_thread_id": 5, "text": "hi"},
        bot=bot,
    )
    assert captured["mirror_to_telegram"] is False
    assert captured["sender_type"] == "user"


# ── Allgemein-Thema → DM-Thread mit Boss ──────────────────────────────────


@pytest.mark.asyncio
async def test_message_without_topic_goes_to_boss_dm(async_session, bot):
    boss = await _boss(async_session)
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "text": "lass uns brainstormen"},
        bot=bot,
    )
    dm = (
        await async_session.exec(
            select(Thread).where(Thread.kind == "dm", Thread.agent_id == boss.id)
        )
    ).first()
    assert dm is not None
    msgs = await _messages(async_session, dm.id)
    assert [m.body for m in msgs] == ["lass uns brainstormen"]


@pytest.mark.asyncio
async def test_general_chat_without_boss_degrades(async_session, bot):
    """Kein Boss-Agent → nicht crashen: kurz Bescheid geben, nichts posten."""
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "text": "hallo?"},
        bot=bot,
    )
    bot.send_message.assert_awaited()  # eine Erklaerung, kein stiller Verlust
    # Kein Thread/Message angelegt.
    assert (await async_session.exec(select(Message))).first() is None


# ── Unbekanntes Thema → nachfragen, nicht raten ───────────────────────────


@pytest.mark.asyncio
async def test_unknown_topic_asks_back_without_guessing(async_session, bot):
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "message_thread_id": 4242, "text": "und hier?"},
        bot=bot,
    )
    # Keine Nachricht gepostet (nichts geraten).
    assert (await async_session.exec(select(Message))).first() is None
    # Rueckfrage im selben Thema.
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs.get("message_thread_id") == 4242


# ── Sicherheit: Fremd-Chat ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_chat_is_ignored(async_session, bot):
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": 99999}, "message_thread_id": 1, "text": "gib mir alles"},
        bot=bot,
    )
    bot.send_message.assert_not_called()  # NIE an Fremde antworten
    assert (await async_session.exec(select(Message))).first() is None


@pytest.mark.asyncio
async def test_missing_chat_is_ignored(async_session, bot):
    await ingest_inbound_message(async_session, {"text": "hi"}, bot=bot)
    bot.send_message.assert_not_called()
    assert (await async_session.exec(select(Message))).first() is None


# ── Sprachnachricht → STT → identischer Weg ───────────────────────────────


@pytest.mark.asyncio
async def test_voice_is_transcribed_into_the_thread(async_session, bot):
    thread = await _thread_with_topic(async_session, 8)
    transcribe = AsyncMock(return_value="deploy den branch")
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "message_thread_id": 8, "voice": {"file_id": "AbC"}},
        bot=bot,
        transcribe=transcribe,
    )
    bot.get_file_bytes.assert_awaited_once_with("AbC")
    transcribe.assert_awaited_once_with(b"\x00oggbytes")
    msgs = await _messages(async_session, thread.id)
    assert [m.body for m in msgs] == ["deploy den branch"]


@pytest.mark.asyncio
async def test_voice_without_transcriber_degrades(async_session, bot):
    thread = await _thread_with_topic(async_session, 9)
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "message_thread_id": 9, "voice": {"file_id": "x"}},
        bot=bot,
        transcribe=None,
    )
    assert await _messages(async_session, thread.id) == []
    bot.send_message.assert_awaited()  # kurz Bescheid, kein stiller Verlust


@pytest.mark.asyncio
async def test_voice_transcription_failure_asks_retry(async_session, bot):
    thread = await _thread_with_topic(async_session, 10)
    transcribe = AsyncMock(side_effect=RuntimeError("stt down"))
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "message_thread_id": 10, "voice": {"file_id": "x"}},
        bot=bot,
        transcribe=transcribe,
    )
    assert await _messages(async_session, thread.id) == []
    reply = bot.send_message.await_args.args[0]
    assert "nicht verstehen" in reply or "nochmal" in reply


# ── Andere Medien ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unsupported_media_gets_reply(async_session, bot):
    thread = await _thread_with_topic(async_session, 11)
    await ingest_inbound_message(
        async_session,
        {"chat": {"id": int(CHAT_ID)}, "message_thread_id": 11, "photo": [{"file_id": "P"}]},
        bot=bot,
    )
    assert await _messages(async_session, thread.id) == []
    reply = bot.send_message.await_args.args[0]
    assert "Text- und Sprachnachrichten" in reply
