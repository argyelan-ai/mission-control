"""send_message um message_thread_id + disable_notification erweitern (P2.3).

Der fehlende erste Parameter ist der belegte Grund, warum Jarvis' Antwort im
Hauptchat landete statt im Thema (Live-Befund 27.07.). Diese Tests fahren ohne
Netz: der httpx-Client wird gefaelscht, wir pruefen nur das erzeugte Payload.
Bestehende Aufrufer duerfen sich nicht aendern → Default-Werte.
"""
import pytest

from app.config import settings
from app.services.telegram_bot import telegram_bot


class _FakeResp:
    def json(self):
        return {"ok": True, "result": {"message_id": 42}}


class _CapturingClient:
    def __init__(self):
        self.data = None

    async def post(self, url, data=None, files=None):
        self.data = data
        return _FakeResp()


@pytest.fixture
def capture(monkeypatch):
    client = _CapturingClient()

    async def _fake_get_client():
        return client

    monkeypatch.setattr(telegram_bot, "_get_client", _fake_get_client)
    monkeypatch.setattr(settings, "telegram_chat_id", "123", raising=False)
    return client


@pytest.mark.asyncio
async def test_send_message_forwards_thread_id_and_silence(capture):
    mid = await telegram_bot.send_message(
        "Rex: fertig", message_thread_id=555, disable_notification=True
    )
    assert mid == 42
    assert capture.data["message_thread_id"] == 555
    assert capture.data["disable_notification"] is True


@pytest.mark.asyncio
async def test_send_message_defaults_are_backward_compatible(capture):
    """Ohne die neuen Argumente bleibt das Payload byte-identisch zu vorher —
    kein message_thread_id, kein disable_notification."""
    mid = await telegram_bot.send_message("hallo")
    assert mid == 42
    assert "message_thread_id" not in capture.data
    assert "disable_notification" not in capture.data


@pytest.mark.asyncio
async def test_send_message_general_topic_zero_is_omitted(capture):
    """Das Allgemein-Thema (Sentinel 0) darf nicht als message_thread_id gesendet
    werden — 0 ist kein gueltiges Telegram-Thema. Der Parameter faellt weg."""
    await telegram_bot.send_message("System: watchdog", message_thread_id=0)
    assert "message_thread_id" not in capture.data
