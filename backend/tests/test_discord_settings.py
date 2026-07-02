"""Phase 29-01 Task 1: Settings.discord_guild_id + discord_category_id.

These two fields are added in Phase 29 (ADR-039 — OpenClaw Gateway Sunset)
because the new `routers/discord.py` reads Discord guild + category IDs
directly from settings/env instead of from the `gateways` DB row.

Defaults are empty strings so the backend boots cleanly with no .env values
set; the operator copies the live values from the legacy `gateways` row into `.env`
(`DISCORD_GUILD_ID=...` + `DISCORD_CATEGORY_ID=...`) before the smoke test.
"""

import os

import pytest

import app.config


def _fresh_settings_class():
    """Return the Settings class for constructing fresh instances.

    No module reload — importlib.reload(app.config) breaks the module-level
    `settings` singleton that other modules (vault.py, etc.) captured via
    `from app.config import settings`. Creating a new Settings(_env_file=None)
    is sufficient to test field defaults and env-var pickup.
    """
    return app.config.Settings


def test_discord_guild_id_default_empty(monkeypatch):
    """Settings().discord_guild_id defaults to empty string (no env, no .env)."""
    # Strip any ambient env vars that would shadow the default
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    Settings = _fresh_settings_class()
    s = Settings(_env_file=None)  # do not read project .env during this test
    assert hasattr(s, "discord_guild_id"), "Settings must expose discord_guild_id"
    assert s.discord_guild_id == "", "default must be empty string"


def test_discord_category_id_default_empty(monkeypatch):
    """Settings().discord_category_id defaults to empty string."""
    monkeypatch.delenv("DISCORD_CATEGORY_ID", raising=False)
    Settings = _fresh_settings_class()
    s = Settings(_env_file=None)
    assert hasattr(s, "discord_category_id"), "Settings must expose discord_category_id"
    assert s.discord_category_id == "", "default must be empty string"


def test_discord_guild_id_reads_env(monkeypatch):
    """Settings().discord_guild_id picks up the DISCORD_GUILD_ID env var."""
    monkeypatch.setenv("DISCORD_GUILD_ID", "1234567890")
    Settings = _fresh_settings_class()
    s = Settings(_env_file=None)
    assert s.discord_guild_id == "1234567890"


def test_discord_category_id_reads_env(monkeypatch):
    """Settings().discord_category_id picks up the DISCORD_CATEGORY_ID env var."""
    monkeypatch.setenv("DISCORD_CATEGORY_ID", "9876543210")
    Settings = _fresh_settings_class()
    s = Settings(_env_file=None)
    assert s.discord_category_id == "9876543210"


def test_existing_discord_fields_still_present():
    """Phase 29-01 must NOT remove the pre-existing Discord settings."""
    Settings = _fresh_settings_class()
    s = Settings(_env_file=None)
    assert hasattr(s, "discord_webhook_ops"), "discord_webhook_ops must remain"
    assert hasattr(s, "discord_bot_token"), "discord_bot_token must remain"


def test_settings_instantiates_without_openclaw_envs(monkeypatch):
    """D-14 invariant: backend must boot without OPENCLAW_WS_URL / OPENCLAW_TOKEN.

    Plan 29-09 removed the `openclaw_ws_url` / `openclaw_token` /
    `gateway_url` Settings fields entirely. With pydantic
    `extra="ignore"`, any leftover values in `.env` are silently
    dropped — backend never touches them.
    """
    monkeypatch.delenv("OPENCLAW_WS_URL", raising=False)
    monkeypatch.delenv("OPENCLAW_TOKEN", raising=False)
    Settings = _fresh_settings_class()
    # Must not raise
    s = Settings(_env_file=None)
    # Fields are gone post-29-09; their absence IS the D-14 guarantee.
    assert not hasattr(s, "openclaw_ws_url"), (
        "Plan 29-09 should have removed openclaw_ws_url from Settings"
    )
    assert not hasattr(s, "openclaw_token"), (
        "Plan 29-09 should have removed openclaw_token from Settings"
    )
    assert not hasattr(s, "gateway_url"), (
        "Plan 29-09 should have removed gateway_url from Settings"
    )
