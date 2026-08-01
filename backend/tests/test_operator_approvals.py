"""Approval fan-out — Telegram buttons unchanged, Slack Block Kit alongside.

R4 of the Slack rebuild. The laws these tests pin:

  * ONE shared token pair for every channel — `consume_action_token` deletes
    siblings via `mc:telegram:approval_tokens:{id}`; two pairs would
    overwrite that key and leave live single-use tokens behind after the
    first click.
  * Slack gets Block-Kit URL buttons on the EXISTING quick-resolve routes —
    no Slack interactivity, the phone opens MC directly.
  * Resolution mirrors where the push went: Telegram edits (as before),
    Slack answers the approval message in its thread (✅/❌ — history stays).
  * A channel outage or missing config never raises into task escalation.

All transports and Redis are faked — no network, ever.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import operator_approvals

APPROVAL_ID = uuid.UUID("00000000-0000-0000-0000-00000000a441")


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _FakeTransport:
    def __init__(self, *, ok=True):
        self.calls: list[dict] = []
        self.ok = ok

    async def post_message(self, **kw):
        from app.services.slack_client import SlackPostResult

        self.calls.append(kw)
        if not self.ok:
            return SlackPostResult(ok=False, code="not_in_channel", error="not in channel")
        return SlackPostResult(ok=True, ts="1754000000.000100")


def _telegram_configured(monkeypatch, value: bool):
    """`configured` ist eine Property ohne Setter — auf der Klasse ersetzen."""
    from app.services.telegram_bot import TelegramBotService

    monkeypatch.setattr(
        TelegramBotService, "configured", property(lambda self: value)
    )


def _slack_on(monkeypatch, transport, redis):
    from app.config import settings
    from app.services import slack_client

    monkeypatch.setattr(settings, "slack_approvals_channel", "#mc-approvals", raising=False)
    monkeypatch.setattr(settings, "mc_base_url", "http://mc.test", raising=False)
    monkeypatch.setattr(slack_client, "SlackTransport", lambda: transport)
    monkeypatch.setattr(
        slack_client, "resolve_channel_id", AsyncMock(return_value="C-APPR")
    )
    monkeypatch.setattr(
        "app.redis_client.get_redis", AsyncMock(return_value=redis)
    )
    monkeypatch.setattr(
        "app.services.telegram_bot.get_redis", AsyncMock(return_value=redis)
    )


@pytest.mark.asyncio
async def test_fanout_shares_one_token_pair_across_channels(monkeypatch):
    redis, transport = _FakeRedis(), _FakeTransport()
    _slack_on(monkeypatch, transport, redis)
    telegram = AsyncMock()
    monkeypatch.setattr(
        "app.services.telegram_bot.telegram_bot.send_approval_telegram", telegram
    )
    # Telegram gilt als konfiguriert, damit needs_tokens greift
    _telegram_configured(monkeypatch, True)

    await operator_approvals.send_approval(APPROVAL_ID, "Rex", "Deploy prüfen", "brauche Go")

    telegram.assert_awaited_once()
    tokens = telegram.await_args.kwargs["tokens"]
    assert tokens is not None, "the fan-out must hand Telegram the shared pair"

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["channel"] == "C-APPR", "upload/postMessage side must use the resolved ID"
    urls = [
        el["url"]
        for block in call["blocks"] if block["type"] == "actions"
        for el in block["elements"]
    ]
    assert len(urls) == 2
    assert any(tokens[0] in u for u in urls) and any(tokens[1] in u for u in urls), (
        "the very same token pair must sit in the Slack buttons"
    )
    assert all(f"/approvals/{APPROVAL_ID}/quick-resolve" in u for u in urls)

    # ts-Mapping für die spätere ✅-Reply
    stored = redis.store.get(f"mc:slack:approval:{APPROVAL_ID}")
    assert stored == "C-APPR|1754000000.000100"


@pytest.mark.asyncio
async def test_resolution_answers_in_the_slack_thread(monkeypatch):
    redis, transport = _FakeRedis(), _FakeTransport()
    _slack_on(monkeypatch, transport, redis)
    redis.store[f"mc:slack:approval:{APPROVAL_ID}"] = "C-APPR|1754000000.000100"
    tele_edit = AsyncMock()
    monkeypatch.setattr(
        "app.services.telegram_bot.telegram_bot.update_resolved_telegram", tele_edit
    )

    await operator_approvals.update_resolved(APPROVAL_ID, "approved", "passt")

    tele_edit.assert_awaited_once()
    assert len(transport.calls) == 1
    reply = transport.calls[0]
    assert reply["thread_ts"] == "1754000000.000100"
    assert "✅" in reply["text"] and "approved" in reply["text"] and "passt" in reply["text"]
    assert f"mc:slack:approval:{APPROVAL_ID}" not in redis.store, (
        "the mapping is single-use — resolved means done"
    )


@pytest.mark.asyncio
async def test_rejected_gets_the_cross(monkeypatch):
    redis, transport = _FakeRedis(), _FakeTransport()
    _slack_on(monkeypatch, transport, redis)
    redis.store[f"mc:slack:approval:{APPROVAL_ID}"] = "C-APPR|1754000000.000100"
    monkeypatch.setattr(
        "app.services.telegram_bot.telegram_bot.update_resolved_telegram", AsyncMock()
    )

    await operator_approvals.update_resolved(APPROVAL_ID, "rejected")

    assert "❌" in transport.calls[0]["text"]


@pytest.mark.asyncio
async def test_unconfigured_slack_is_a_silent_no_op(monkeypatch):
    """No approvals channel -> Telegram alone, exactly the pre-R4 behaviour."""
    from app.config import settings

    monkeypatch.setattr(settings, "slack_approvals_channel", "", raising=False)
    telegram = AsyncMock()
    monkeypatch.setattr(
        "app.services.telegram_bot.telegram_bot.send_approval_telegram", telegram
    )
    _telegram_configured(monkeypatch, False)
    transport = _FakeTransport()
    monkeypatch.setattr("app.services.slack_client.SlackTransport", lambda: transport)

    await operator_approvals.send_approval(APPROVAL_ID, "Rex", "T", "B")

    telegram.assert_awaited_once()
    assert telegram.await_args.kwargs["tokens"] is None, (
        "nothing configured -> no Redis round-trip; Telegram's own legacy path decides"
    )
    assert transport.calls == []


@pytest.mark.asyncio
async def test_token_failure_still_pushes_telegram(monkeypatch):
    """A Redis hiccup must cost the Slack buttons at most — never the push."""
    _telegram_configured(monkeypatch, True)
    monkeypatch.setattr(
        "app.services.telegram_bot.create_approval_tokens",
        AsyncMock(side_effect=RuntimeError("redis down")),
    )
    telegram = AsyncMock()
    monkeypatch.setattr(
        "app.services.telegram_bot.telegram_bot.send_approval_telegram", telegram
    )

    await operator_approvals.send_approval(APPROVAL_ID, "Rex", "T", "B")

    telegram.assert_awaited_once()
    assert telegram.await_args.kwargs["tokens"] is None


@pytest.mark.asyncio
async def test_slack_outage_never_raises(monkeypatch):
    redis = _FakeRedis()
    transport = _FakeTransport(ok=False)
    _slack_on(monkeypatch, transport, redis)
    monkeypatch.setattr(
        "app.services.telegram_bot.telegram_bot.send_approval_telegram", AsyncMock()
    )
    _telegram_configured(monkeypatch, True)

    await operator_approvals.send_approval(APPROVAL_ID, "Rex", "T", "B")

    assert redis.store.get(f"mc:slack:approval:{APPROVAL_ID}") is None, (
        "no delivered message -> no ts mapping to answer later"
    )


@pytest.mark.asyncio
async def test_resolution_without_mapping_is_silent(monkeypatch):
    redis, transport = _FakeRedis(), _FakeTransport()
    _slack_on(monkeypatch, transport, redis)
    monkeypatch.setattr(
        "app.services.telegram_bot.telegram_bot.update_resolved_telegram", AsyncMock()
    )

    await operator_approvals.update_resolved(APPROVAL_ID, "approved")

    assert transport.calls == [], "no stored ts -> nothing to answer, no error"
