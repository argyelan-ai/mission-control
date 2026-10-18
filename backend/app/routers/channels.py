"""Channels settings — the operator's switchboard for chat channels.

Admin-only, like every endpoint that touches System-Token state (ADR-033).
GET returns the EFFECTIVE runtime values (env default + DB override already
applied) plus which functions are actually deliverable; PUT saves operator
decisions into ``app_settings`` and applies them to the running process
immediately (services/channel_config.py) — no restart.

Telegram's connection test mirrors ``/slack/test-connection``: always HTTP
200, the failure is a reportable state; the response never contains token
material.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role
from app.config import settings
from app.database import get_session
from app.services.channel_config import (
    CHANNEL_SETTING_FIELDS,
    save_channel_settings,
    stored_overrides,
)

logger = logging.getLogger("mc.channels")

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


class ChannelSettingsUpdate(BaseModel):
    """Partial update; only allowlisted keys, enforced in the service."""

    settings: dict[str, bool | str]


@router.get("/settings")
async def get_channel_settings(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Effective values + override provenance + configured-state per function.

    ``values`` are what the system ACTS on right now; ``overridden`` names the
    keys the operator has pinned via this page (everything else is env/default).
    Tokens never appear here — they live behind the secrets API, masked.
    """
    from app.services.operator_reports import (
        SlackReportsBackend,
        TelegramReportsBackend,
    )
    from app.services.telegram_bot import telegram_bot

    values = {key: getattr(settings, key, None) for key in CHANNEL_SETTING_FIELDS}
    overridden = sorted((await stored_overrides(session)).keys())
    return {
        "values": values,
        "overridden": overridden,
        "state": {
            "telegram_command_bot_configured": bool(
                settings.telegram_bot_token and settings.telegram_chat_id
            ),
            "telegram_reports_configured": TelegramReportsBackend().configured,
            "telegram_approvals_active": bool(
                telegram_bot.configured
                and getattr(settings, "telegram_approvals_enabled", True)
            ),
            "slack_reports_configured": SlackReportsBackend().configured,
        },
    }


@router.put("/settings")
async def put_channel_settings(
    payload: ChannelSettingsUpdate,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Save + apply immediately. 422 on any key outside the allowlist."""
    try:
        applied = await save_channel_settings(session, dict(payload.settings))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    logger.info("channel settings updated: %s", sorted(payload.settings.keys()))
    return {"ok": True, "applied": sorted(payload.settings.keys()), "effective": {
        k: v for k, v in applied.items() if k in payload.settings
    }}


@router.post("/telegram/test-connection")
async def telegram_test_connection(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_role(Role.ADMIN)),
):
    """Check both Telegram bots (command + reports) with getMe. Always 200.

    Mirrors Slack's contract: separate result per token so a broken reports
    bot can never masquerade as a broken command bot. No token material in
    the response. The procedure lives in the check registry
    (``config_checks`` kind ``telegram``), which also stores each result.
    """
    from app.services import config_checks
    from app.services.config_checks.kinds import telegram_unset_entry

    result: dict = {}
    for label in ("command_bot", "reports_bot"):
        try:
            run = await config_checks.run_check(session, "telegram", label)
            result[label] = run.outcome.detail
        except config_checks.CheckNotRunnable:
            result[label] = telegram_unset_entry(label)
    return result
