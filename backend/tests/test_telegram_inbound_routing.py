"""Jarvis-Umzug: der Telegram-Inbound-Poller ist EIN Abnehmer, das Flag lenkt
das Ziel (P2.4).

Bei `telegram_team_chat_enabled=False` bleibt exakt das heutige Verhalten: der
Jarvis-Handler bekommt die Nachricht. Ist das Flag an, geht dieselbe Nachricht in
den Thread-Ingest — kein zweiter getUpdates-Abnehmer, nur ein anderes Ziel im
selben Handler (der Umzug ist atomar).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.telegram_bot import TelegramBotService


@pytest.fixture
def bot():
    return TelegramBotService()


# ── Routing: Flag lenkt Jarvis vs. Thread-Ingest ──────────────────────────


@pytest.mark.asyncio
async def test_flag_off_routes_to_jarvis(bot, monkeypatch):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", False, raising=False)
    bot._jarvis.handle_message = AsyncMock()
    ingest = AsyncMock()
    monkeypatch.setattr(bot, "_ingest_to_thread", ingest)

    await bot._handle_inbound_message({"chat": {"id": 1}, "text": "hi"})

    bot._jarvis.handle_message.assert_awaited_once()
    ingest.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_routes_to_thread_ingest(bot, monkeypatch):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    bot._jarvis.handle_message = AsyncMock()
    ingest = AsyncMock()
    monkeypatch.setattr(bot, "_ingest_to_thread", ingest)

    await bot._handle_inbound_message({"chat": {"id": 1}, "text": "hi"})

    ingest.assert_awaited_once()
    bot._jarvis.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_error_never_breaks_the_poll_loop(bot, monkeypatch):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(bot, "_ingest_to_thread", AsyncMock(side_effect=RuntimeError("boom")))
    # Darf nicht werfen — der Handler isoliert den Fehler.
    await bot._handle_inbound_message({"chat": {"id": 1}, "text": "hi"})


# ── start(): der Poller laeuft auch fuer den Team-Chat (nicht nur Jarvis) ──


@pytest.mark.asyncio
async def test_start_polls_for_team_chat_even_without_jarvis(bot, monkeypatch):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "jarvis_telegram_enabled", False, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "x", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)
    monkeypatch.setattr(bot, "_poll_loop", AsyncMock())

    await bot.start()
    try:
        assert bot._running is True
        assert bot._task is not None
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_start_stays_idle_when_both_disabled(bot, monkeypatch):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", False, raising=False)
    monkeypatch.setattr(settings, "jarvis_telegram_enabled", False, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "x", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "1", raising=False)
    monkeypatch.setattr(bot, "_poll_loop", AsyncMock())

    await bot.start()
    assert bot._running is False
    assert bot._task is None


@pytest.mark.asyncio
async def test_start_skips_when_team_chat_on_but_unconfigured(bot, monkeypatch):
    monkeypatch.setattr(settings, "telegram_team_chat_enabled", True, raising=False)
    monkeypatch.setattr(settings, "jarvis_telegram_enabled", False, raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_chat_id", "", raising=False)
    monkeypatch.setattr(bot, "_poll_loop", AsyncMock())

    await bot.start()
    assert bot._running is False


# ── Produktions-Transkriber ist an dieselbe STT-Kette gebunden ────────────


def test_voice_transcriber_none_without_openai_key(bot, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    assert bot._voice_transcriber() is None
