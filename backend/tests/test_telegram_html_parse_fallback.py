"""HTML-Parse-Fallback fuer send_message (P2.3-Nachbesserung).

Agenten schreiben staendig Code/Vergleiche mit rohem `<`, `<div>`, `a < b`.
Telegram lehnt das im HTML-Parse-Modus mit 400 "can't parse entities" ab — die
Nachricht ginge sonst still verloren (geloggt, nie zugestellt). Der Fallback
sendet denselben Text bei einer Parse-400 OHNE parse_mode erneut: Formatierung
bleibt wo sie funktioniert, Zustellung ist garantiert. Kein Netz — httpx
gefaelscht.
"""
import pytest

from app.config import settings
from app.services.telegram_bot import telegram_bot


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _ParseFailThenOk:
    """Erster Aufruf (mit parse_mode) → 400 Parse-Fehler; zweiter (ohne) → ok."""

    def __init__(self):
        self.calls: list[dict] = []

    async def post(self, url, data=None, files=None):
        self.calls.append(dict(data))
        if "parse_mode" in data:
            return _Resp({
                "ok": False,
                "error_code": 400,
                "description": (
                    'Bad Request: can\'t parse entities: Unsupported start tag '
                    '"div" at byte offset 17'
                ),
            })
        return _Resp({"ok": True, "result": {"message_id": 77}})


class _Non400Fail:
    """400-fremder Fehler (z.B. Thema geschlossen) → KEIN Fallback, ein Aufruf."""

    def __init__(self):
        self.calls: list[dict] = []

    async def post(self, url, data=None, files=None):
        self.calls.append(dict(data))
        return _Resp({
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message thread not found",
        })


def _patch(monkeypatch, client):
    async def _fake_get_client():
        return client

    monkeypatch.setattr(telegram_bot, "_get_client", _fake_get_client)
    monkeypatch.setattr(settings, "telegram_chat_id", "123", raising=False)


@pytest.mark.asyncio
async def test_raw_angle_bracket_delivered_via_plaintext_retry(monkeypatch):
    client = _ParseFailThenOk()
    _patch(monkeypatch, client)

    body = "Rex: if a < b then <div> gilt"
    mid = await telegram_bot.send_message(body)

    assert mid == 77, "Nachricht muss ankommen, nicht still verloren gehen"
    assert len(client.calls) == 2, "genau ein Fallback-Retry"
    assert "parse_mode" in client.calls[0]
    assert "parse_mode" not in client.calls[1], "Retry ohne parse_mode"
    assert client.calls[1]["text"] == body, "identischer Text, nur ohne Formatierung"


@pytest.mark.asyncio
async def test_thread_id_and_silence_survive_the_retry(monkeypatch):
    """Der Fallback darf Routing (Thema) und Stille nicht verlieren."""
    client = _ParseFailThenOk()
    _patch(monkeypatch, client)

    await telegram_bot.send_message(
        "a < b", message_thread_id=555, disable_notification=True
    )
    assert client.calls[1]["message_thread_id"] == 555
    assert client.calls[1]["disable_notification"] is True


@pytest.mark.asyncio
async def test_non_parse_400_does_not_retry(monkeypatch):
    """Ein 400, das nichts mit Parsing zu tun hat, wird NICHT ohne parse_mode
    wiederholt — der Retry wuerde ebenso scheitern und nur doppelt loggen."""
    client = _Non400Fail()
    _patch(monkeypatch, client)

    mid = await telegram_bot.send_message("hallo")
    assert mid is None
    assert len(client.calls) == 1, "kein Fallback bei Nicht-Parse-Fehler"
