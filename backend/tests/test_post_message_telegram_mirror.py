"""post_message feuert den Telegram-Spiegel (P2.3) — best-effort, hinter Flag.

Der Spiegel ist inert, solange telegram_team_chat_enabled aus ist (Default) →
die ~3900 bestehenden Tests bleiben unberuehrt. Ist er an, wird jede Nachricht
gespiegelt, ausser der Aufrufer setzt mirror_to_telegram=False (Schleifenschutz,
den P2.4 beim Telegram-Ingest nutzt).
"""
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.thread import Thread
from app.services.messaging import post_message


async def _thread(session) -> Thread:
    t = Thread(kind="task")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@pytest.fixture
def mirror_spy(monkeypatch):
    calls: list[str] = []

    async def spy(session, message, *, topic_client, bot, now=None):
        calls.append(message.body)
        return True

    monkeypatch.setattr(
        "app.services.telegram_outbound.mirror_message_to_telegram", spy
    )
    return calls


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "x", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)


@pytest.mark.asyncio
async def test_mirror_fires_when_enabled(async_session: AsyncSession, monkeypatch, mirror_spy):
    _enable(monkeypatch)
    thread = await _thread(async_session)

    await post_message(
        async_session, thread_id=thread.id, sender_type="agent", body="hallo"
    )

    assert mirror_spy == ["hallo"]


@pytest.mark.asyncio
async def test_no_mirror_when_disabled(async_session: AsyncSession, monkeypatch, mirror_spy):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", False, raising=False)
    thread = await _thread(async_session)

    await post_message(
        async_session, thread_id=thread.id, sender_type="agent", body="hallo"
    )

    assert mirror_spy == []


@pytest.mark.asyncio
async def test_mirror_to_telegram_false_suppresses_mirror(async_session: AsyncSession, monkeypatch, mirror_spy):
    """Der Schleifenschutz-Schalter, den P2.4 beim Telegram-Ingest setzt."""
    _enable(monkeypatch)
    thread = await _thread(async_session)

    await post_message(
        async_session, thread_id=thread.id, sender_type="user",
        body="aus Telegram", mirror_to_telegram=False,
    )

    assert mirror_spy == []


@pytest.mark.asyncio
async def test_mirror_error_never_breaks_post_message(async_session: AsyncSession, monkeypatch):
    """Ein Fehler im Spiegel-Pfad darf post_message nie zum Scheitern bringen."""
    _enable(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("telegram exploded")

    monkeypatch.setattr(
        "app.services.telegram_outbound.mirror_message_to_telegram", boom
    )
    thread = await _thread(async_session)

    msg = await post_message(
        async_session, thread_id=thread.id, sender_type="agent", body="trotzdem"
    )

    assert msg.body == "trotzdem"  # Nachricht persistiert, kein Fehler durchgereicht
