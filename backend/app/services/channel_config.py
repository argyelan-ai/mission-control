"""Runtime channel configuration — the engine behind the channels settings page.

Three layers, in override order:

  1. **pydantic env defaults** (`app/config.py`) — a plain .env install keeps
     working exactly as before; env is the default, never the master.
  2. **app_settings rows** (operator decisions from the settings UI) —
     applied onto the live ``settings`` singleton, so every existing
     ``settings.telegram_chat_id``-style read site works unchanged.
  3. **secrets-stored Telegram tokens** (ADR-033, Fernet) — decrypted and
     applied the same way, mirroring how Slack keeps its tokens in the
     ``secrets`` table instead of .env.

The singleton patch is sound because the backend runs a single uvicorn
worker (Dockerfile ``--workers 1``); ``apply_channel_overrides`` runs at
startup (before the chat loops) and after every settings/token save, so a
change takes effect without a restart.

Only keys in ``CHANNEL_SETTING_FIELDS`` may be overridden — the allowlist is
the security boundary (an admin must not be able to override arbitrary
Settings fields such as crypto keys through this path).
"""
from __future__ import annotations

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.app_setting import AppSetting

logger = logging.getLogger("mc.channel_config")

# key -> python type of the Settings field with the same name.
# Every key here must exist on app.config.Settings.
CHANNEL_SETTING_FIELDS: dict[str, type] = {
    # Telegram — targets
    "telegram_chat_id": str,
    "telegram_reports_chat_id": str,
    # Telegram — per-function toggles
    "telegram_team_chat_enabled": bool,
    "telegram_reports_enabled": bool,
    "telegram_approvals_enabled": bool,
    "jarvis_telegram_enabled": bool,
    # Slack — targets
    "slack_default_channel": str,
    "slack_reports_channel": str,
    "slack_approvals_channel": str,
    # Slack — per-function toggles
    "slack_team_chat_enabled": bool,
    "slack_reports_enabled": bool,
    "slack_approvals_enabled": bool,
    # Which chat adapters mirror the team chat (comma list, "" = all enabled)
    "chat_channels": str,
}

# Telegram bot tokens live in the secrets table (like Slack's, ADR-033) and
# are applied onto the settings singleton so every existing read site
# (telegram_bot.py builds its URL per call) keeps working unchanged.
TELEGRAM_TOKEN_SECRET_KEYS: tuple[str, ...] = (
    "telegram_bot_token",
    "telegram_reports_bot_token",
)


def _coerce(key: str, raw: str) -> object:
    kind = CHANNEL_SETTING_FIELDS[key]
    if kind is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return raw


def _serialize(key: str, value: object) -> str:
    kind = CHANNEL_SETTING_FIELDS[key]
    if kind is bool:
        return "true" if bool(value) else "false"
    return str(value)


async def stored_overrides(session: AsyncSession) -> dict[str, object]:
    """The operator's saved decisions, typed. Unknown keys are skipped with a
    warning (an old row must never break startup)."""
    from app.services.ai_provider_config import AI_PROVIDER_SETTING_FIELDS

    rows = (await session.exec(select(AppSetting))).all()
    out: dict[str, object] = {}
    for row in rows:
        if row.key not in CHANNEL_SETTING_FIELDS:
            # app_settings is one KV table shared by several settings pages.
            # A key another page owns is not "unknown" — only warn for rows no
            # allowlist claims, otherwise every AI-provider row logs a warning.
            if row.key not in AI_PROVIDER_SETTING_FIELDS:
                logger.warning("app_settings: unbekannter Key %r ignoriert", row.key)
            continue
        out[row.key] = _coerce(row.key, row.value)
    return out


async def apply_channel_overrides(session: AsyncSession) -> dict[str, object]:
    """DB state -> live ``settings`` singleton. Returns what was applied.

    Never raises: a broken row or a secrets hiccup degrades to "env value
    stays", logged — channel config must not take the backend down.
    """
    applied: dict[str, object] = {}
    try:
        for key, value in (await stored_overrides(session)).items():
            setattr(settings, key, value)
            applied[key] = value
    except Exception as e:  # noqa: BLE001 — degrade to env defaults
        logger.warning("channel overrides nicht anwendbar: %s", e)

    try:
        from app.services.secrets_helper import get_secret_plaintext_by_key

        for key in TELEGRAM_TOKEN_SECRET_KEYS:
            token = await get_secret_plaintext_by_key(session, key)
            if token:
                setattr(settings, key, token)
                applied[key] = "<secret>"
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram token overrides nicht anwendbar: %s", e)
    return applied


async def save_channel_settings(
    session: AsyncSession, updates: dict[str, object]
) -> dict[str, object]:
    """Validate against the allowlist, upsert, apply. Returns applied values.

    Raises ValueError on a key outside the allowlist — the router turns that
    into a 422; nothing is written in that case (all-or-nothing).
    """
    unknown = sorted(set(updates) - set(CHANNEL_SETTING_FIELDS))
    if unknown:
        raise ValueError(f"Unbekannte Channel-Settings: {', '.join(unknown)}")

    for key, value in updates.items():
        serialized = _serialize(key, value)
        row = (
            await session.exec(select(AppSetting).where(AppSetting.key == key))
        ).one_or_none()
        if row is None:
            session.add(AppSetting(key=key, value=serialized))
        else:
            row.value = serialized
            session.add(row)
    await session.commit()
    return await apply_channel_overrides(session)
