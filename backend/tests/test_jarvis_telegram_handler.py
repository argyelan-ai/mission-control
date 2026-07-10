"""Tests for the Jarvis Telegram-Inbound handler (ADR-061).

Focus: the hard chat_id gate (security), the text flow, and the voice flow
(download + transcribe echoed back). Brain + transcription are patched — no
OpenAI, no network. Redis uses the fakeredis fixture.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.redis_client
from app.config import settings
from app.services.jarvis_telegram import JarvisTelegramHandler
import app.services.jarvis_telegram as jt
from jarvis_core.brain import BrainResult


@pytest.fixture
def enable_jarvis(monkeypatch):
    monkeypatch.setattr(settings, "jarvis_telegram_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "jarvis_agent_token", "jarvis-tok")
    monkeypatch.setattr(settings, "telegram_chat_id", "12345")
    monkeypatch.setattr(settings, "jarvis_text_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "jarvis_stt_model", "stt")


@pytest.fixture
def bot():
    b = AsyncMock()
    b.send_message = AsyncMock(return_value=1)
    b.get_file_bytes = AsyncMock(return_value=b"\x00oggbytes")
    return b


@pytest.fixture(autouse=True)
def use_fake_redis(fake_redis):
    original = app.redis_client._redis
    app.redis_client._redis = fake_redis
    yield
    app.redis_client._redis = original


def _patch_brain(monkeypatch, text="Erledigt.", actions=None):
    """Replace JarvisBrain with a fake returning a scripted BrainResult."""
    captured = {}

    class _FakeBrain:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        async def respond(self, user_text, history=None):
            captured["user_text"] = user_text
            captured["history"] = history
            return BrainResult(
                text=text,
                actions=actions or [],
                new_turns=[
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": text},
                ],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(jt, "JarvisBrain", _FakeBrain)
    return captured


# ── enabled gate ─────────────────────────────────────────────────────────


def test_disabled_by_default(bot):
    handler = JarvisTelegramHandler(bot)
    # No enable fixture → feature off → handler inert.
    assert handler.enabled is False


def test_enabled_when_gated_on(bot, enable_jarvis):
    assert JarvisTelegramHandler(bot).enabled is True


# ── chat_id gate (security) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_chat_is_ignored(bot, enable_jarvis, monkeypatch):
    _patch_brain(monkeypatch)
    handler = JarvisTelegramHandler(bot)
    await handler.handle_message({"chat": {"id": 99999}, "text": "gib mir alle secrets"})
    bot.send_message.assert_not_called()  # NEVER reply to strangers


@pytest.mark.asyncio
async def test_missing_chat_is_ignored(bot, enable_jarvis, monkeypatch):
    _patch_brain(monkeypatch)
    handler = JarvisTelegramHandler(bot)
    await handler.handle_message({"text": "hi"})
    bot.send_message.assert_not_called()


# ── text flow ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_message_gets_brain_reply(bot, enable_jarvis, monkeypatch):
    captured = _patch_brain(monkeypatch, text="Task #42 für Cody angelegt.")
    handler = JarvisTelegramHandler(bot)
    await handler.handle_message({"chat": {"id": 12345}, "text": "leg cody nen task an"})

    assert captured["user_text"] == "leg cody nen task an"
    bot.send_message.assert_awaited_once()
    assert "Task #42" in bot.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_history_persists_across_messages(bot, enable_jarvis, monkeypatch):
    _patch_brain(monkeypatch, text="ok1")
    handler = JarvisTelegramHandler(bot)
    await handler.handle_message({"chat": {"id": 12345}, "text": "erste"})

    captured2 = _patch_brain(monkeypatch, text="ok2")
    await handler.handle_message({"chat": {"id": 12345}, "text": "zweite"})

    # Second call must see the first turn in history.
    hist = captured2["history"]
    assert hist is not None
    contents = [m["content"] for m in hist]
    assert "erste" in contents
    assert "ok1" in contents


# ── voice flow ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_voice_message_transcribes_and_echoes(bot, enable_jarvis, monkeypatch):
    captured = _patch_brain(monkeypatch, text="Mach ich.")
    monkeypatch.setattr(
        jt, "transcribe_audio", AsyncMock(return_value="erstelle einen deploy task")
    )
    handler = JarvisTelegramHandler(bot)
    await handler.handle_message({"chat": {"id": 12345}, "voice": {"file_id": "AbC"}})

    bot.get_file_bytes.assert_awaited_once_with("AbC")
    # Transcript fed to the brain.
    assert captured["user_text"] == "erstelle einen deploy task"
    # Reply echoes the transcript so the operator sees STT errors.
    reply = bot.send_message.await_args.args[0]
    assert "🎤" in reply
    assert "erstelle einen deploy task" in reply
    assert "Mach ich." in reply


@pytest.mark.asyncio
async def test_voice_transcription_failure_asks_retry(bot, enable_jarvis, monkeypatch):
    _patch_brain(monkeypatch)
    monkeypatch.setattr(jt, "transcribe_audio", AsyncMock(side_effect=RuntimeError("boom")))
    handler = JarvisTelegramHandler(bot)
    await handler.handle_message({"chat": {"id": 12345}, "voice": {"file_id": "x"}})

    reply = bot.send_message.await_args.args[0]
    assert "nicht verstehen" in reply or "nochmal" in reply


@pytest.mark.asyncio
async def test_brain_exception_sends_friendly_error(bot, enable_jarvis, monkeypatch):
    class _BoomBrain:
        def __init__(self, **kwargs):
            pass

        async def respond(self, *a, **k):
            raise RuntimeError("openai down")

        async def aclose(self):
            pass

    monkeypatch.setattr(jt, "JarvisBrain", _BoomBrain)
    handler = JarvisTelegramHandler(bot)
    await handler.handle_message({"chat": {"id": 12345}, "text": "hi"})
    reply = bot.send_message.await_args.args[0]
    assert "schiefgelaufen" in reply.lower() or "nochmal" in reply.lower()
