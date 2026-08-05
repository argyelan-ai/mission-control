"""Channels settings page — DB-backed runtime channel config.

Pins the three-layer contract (env default -> app_settings override ->
secrets-stored Telegram tokens), the per-function toggles in both fan-outs,
and the security boundary (allowlist, admin-only, no token material in any
response). Also the import-freeze regression: a Telegram token set at
runtime must take effect without a backend restart.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import Settings, settings
from tests.conftest import test_engine


@pytest.fixture(autouse=True)
def _restore_settings():
    """Every test may patch the live singleton — put the env defaults back."""
    keys = [
        "telegram_bot_token", "telegram_chat_id",
        "telegram_reports_bot_token", "telegram_reports_chat_id",
        "telegram_reports_enabled", "telegram_approvals_enabled",
        "telegram_team_chat_enabled", "jarvis_telegram_enabled",
        "slack_default_channel", "slack_reports_channel",
        "slack_approvals_channel", "slack_team_chat_enabled",
        "slack_reports_enabled", "slack_approvals_enabled", "chat_channels",
    ]
    before = {k: getattr(settings, k) for k in keys}
    yield
    for k, v in before.items():
        setattr(settings, k, v)


async def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


# ── 1. Service: save + apply ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_saved_setting_overrides_the_running_config():
    from app.services.channel_config import save_channel_settings

    assert settings.telegram_reports_enabled is True
    async with await _session() as s:
        await save_channel_settings(s, {"telegram_reports_enabled": False})
    assert settings.telegram_reports_enabled is False


@pytest.mark.asyncio
async def test_unknown_key_is_rejected_and_nothing_is_written():
    from sqlmodel import select

    from app.models.app_setting import AppSetting
    from app.services.channel_config import save_channel_settings

    async with await _session() as s:
        with pytest.raises(ValueError):
            await save_channel_settings(
                s, {"secret_key": "boom", "slack_reports_enabled": False}
            )
        assert (await s.exec(select(AppSetting))).all() == []
    assert settings.slack_reports_enabled is True


@pytest.mark.asyncio
async def test_apply_without_rows_keeps_env_defaults():
    from app.services.channel_config import apply_channel_overrides

    env_default = Settings().slack_reports_channel
    async with await _session() as s:
        await apply_channel_overrides(s)
    assert settings.slack_reports_channel == env_default


@pytest.mark.asyncio
async def test_secret_stored_telegram_token_reaches_the_singleton():
    from app.services.channel_config import apply_channel_overrides
    from app.services.secrets_helper import upsert_secret_by_key

    async with await _session() as s:
        await upsert_secret_by_key(
            s, key="telegram_reports_bot_token", value="999:AA-secret-token"
        )
        await apply_channel_overrides(s)
    assert settings.telegram_reports_bot_token == "999:AA-secret-token"


# ── 2. The toggles gate the fan-outs ─────────────────────────────────────


@pytest.mark.asyncio
async def test_reports_toggle_switches_a_configured_telegram_off():
    from app.services.operator_reports import TelegramReportsBackend

    settings.telegram_reports_bot_token = "111:AA"
    settings.telegram_reports_chat_id = "42"
    assert TelegramReportsBackend().configured is True
    settings.telegram_reports_enabled = False
    assert TelegramReportsBackend().configured is False


@pytest.mark.asyncio
async def test_reports_toggle_switches_a_configured_slack_off():
    from app.services.operator_reports import SlackReportsBackend

    settings.slack_reports_channel = "#mc-reports"
    assert SlackReportsBackend().configured is True
    settings.slack_reports_enabled = False
    assert SlackReportsBackend().configured is False


@pytest.mark.asyncio
async def test_approvals_toggle_silences_the_slack_leg():
    from app.services.operator_approvals import _slack_channel

    settings.slack_approvals_channel = "#mc-approvals"
    assert _slack_channel() == "#mc-approvals"
    settings.slack_approvals_enabled = False
    assert _slack_channel() == ""


@pytest.mark.asyncio
async def test_approvals_toggle_silences_the_telegram_leg(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import operator_approvals, telegram_bot as tb_module

    sent = AsyncMock()
    monkeypatch.setattr(tb_module.telegram_bot, "send_approval_telegram", sent)
    monkeypatch.setattr(
        operator_approvals, "create_approval_tokens", AsyncMock(return_value=None),
        raising=False,
    )
    settings.telegram_approvals_enabled = False
    await operator_approvals.send_approval(uuid.uuid4(), "Rex", "T", "blocked")
    sent.assert_not_awaited()


# ── 3. Import-freeze regression ──────────────────────────────────────────


def test_reports_bot_reads_its_token_at_call_time():
    """The old singleton froze the token at import — a settings-page save
    silently required a restart. Now it must see the change immediately."""
    from app.services.telegram_reports import telegram_reports

    settings.telegram_reports_bot_token = ""
    settings.telegram_reports_chat_id = ""
    assert telegram_reports.configured is False
    settings.telegram_reports_bot_token = "222:BB"
    settings.telegram_reports_chat_id = "77"
    assert telegram_reports.configured is True


# ── 4. Endpoints ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_returns_values_without_token_material(
    auth_client: AsyncClient,
):
    settings.telegram_bot_token = "333:CC-super-geheim"
    resp = await auth_client.get("/api/v1/channels/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "telegram_reports_enabled" in body["values"]
    assert "333:CC" not in resp.text  # niemals Token-Material


@pytest.mark.asyncio
async def test_put_settings_applies_immediately(auth_client: AsyncClient):
    resp = await auth_client.put(
        "/api/v1/channels/settings",
        json={"settings": {"slack_approvals_enabled": False}},
    )
    assert resp.status_code == 200, resp.text
    assert settings.slack_approvals_enabled is False


@pytest.mark.asyncio
async def test_put_settings_rejects_unknown_keys(auth_client: AsyncClient):
    resp = await auth_client.put(
        "/api/v1/channels/settings",
        json={"settings": {"secret_key": "nope"}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_channels_endpoints_require_admin(client: AsyncClient):
    from app.auth import create_access_token
    from app.models.user import User

    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            User(
                id=user_id, email="viewer2@mc.local", name="V", role="viewer",
                is_active=True,
            )
        )
        await s.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(user_id), 'viewer')}"}
    assert (
        await client.get("/api/v1/channels/settings", headers=headers)
    ).status_code == 403
    assert (
        await client.put(
            "/api/v1/channels/settings",
            json={"settings": {}},
            headers=headers,
        )
    ).status_code == 403
    assert (
        await client.post("/api/v1/channels/telegram/test-connection", headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_telegram_test_connection_reports_both_bots(
    auth_client: AsyncClient, monkeypatch
):
    from app.routers import channels as channels_router

    async def fake_get_me(token: str) -> dict:
        if token == "ok-token":
            return {"ok": True, "result": {"username": "mc_bot"}}
        return {"ok": False, "description": "Unauthorized"}

    monkeypatch.setattr(channels_router, "_get_me", fake_get_me)
    settings.telegram_bot_token = "ok-token"
    settings.telegram_chat_id = "42"
    settings.telegram_reports_bot_token = "bad-token"
    settings.telegram_reports_chat_id = ""

    resp = await auth_client.post("/api/v1/channels/telegram/test-connection")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["command_bot"]["connected"] is True
    assert body["command_bot"]["bot_username"] == "mc_bot"
    assert body["reports_bot"]["connected"] is False
    assert body["reports_bot"]["error"] == "Unauthorized"
    assert body["reports_bot"]["chat_id_set"] is False
    assert "ok-token" not in resp.text and "bad-token" not in resp.text