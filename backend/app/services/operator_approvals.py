"""Operator approvals — channel-neutral push + resolution fan-out.

The approval push ("Agent X is blocked, decide") was wired straight into the
Telegram bot, five callsites deep. This module is the seam, the same shape as
:mod:`operator_reports` (R1): callsites talk to :func:`send_approval` /
:func:`update_resolved`, and every configured channel delivers — Telegram's
URL buttons unchanged, Slack's Block-Kit buttons in ``#mc-approvals``
alongside them. Telegram stays a first-class optional channel by the
operator's decision (2026-08-01) — this is a fan-out, not a migration.

Design rules (inherited from operator_reports, learned the hard way):

* **One token pair for all channels.** ``consume_action_token`` deletes the
  sibling pair via ``mc:telegram:approval_tokens:{id}`` — a second pair per
  channel would overwrite that key and leave stale single-use tokens alive
  after the first click. The fan-out creates the pair ONCE and hands it to
  every channel.
* **Config is read at call time**, never at import.
* **Never throw into the caller's flow.** An approval push is a notification;
  a channel outage must not fail task escalation. Every failure is logged,
  none is raised.
* **Resolution mirrors where the push went.** Telegram edits its message
  (existing behaviour); Slack answers the approval message with a ✅/❌
  thread reply — history stays visible (operator's decision, §7.3).

The Slack message ts travels through Redis exactly like Telegram's
message_id (same TTL) — an approval older than two days has timed out
anyway, nothing to edit.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger("mc.operator_approvals")

_REDIS_TTL = 172800  # 2 days — same window as the Telegram message mapping


def _slack_redis_key(approval_id: str) -> str:
    return f"mc:slack:approval:{approval_id}"


def _slack_channel() -> str:
    from app.config import settings

    # Toggle first (channels settings page): a configured but switched-off
    # function behaves exactly like an unconfigured one.
    if not getattr(settings, "slack_approvals_enabled", True):
        return ""
    return (getattr(settings, "slack_approvals_channel", "") or "").strip()


async def send_approval(
    approval_id: uuid.UUID,
    agent_name: str,
    task_title: str,
    blocker_comment: str,
) -> None:
    """Push one approval to every configured channel. Never raises."""
    tokens = None
    try:
        from app.services.telegram_bot import create_approval_tokens, telegram_bot

        needs_tokens = telegram_bot.configured or bool(_slack_channel())
        if needs_tokens:
            tokens = await create_approval_tokens(approval_id)
    except Exception as e:  # noqa: BLE001 — Telegram erzeugt notfalls selbst
        # ein Paar (tokens=None, Legacy-Pfad); nur Slack bleibt dann ohne
        # Buttons. Ein Redis-Schluckauf darf den Push nicht verhindern.
        logger.warning("approval tokens for %s failed: %s", approval_id, e)

    try:
        from app.config import settings
        from app.services.telegram_bot import telegram_bot

        # Toggle (channels settings page): configured-but-off behaves like
        # unconfigured — the Slack leg stays independent.
        if getattr(settings, "telegram_approvals_enabled", True):
            await telegram_bot.send_approval_telegram(
                approval_id, agent_name, task_title, blocker_comment, tokens=tokens
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram approval push %s failed: %s", approval_id, e)

    try:
        await _send_approval_slack(
            approval_id, agent_name, task_title, blocker_comment, tokens
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("slack approval push %s failed: %s", approval_id, e)


async def update_resolved(
    approval_id: uuid.UUID, status: str, resolver_note: str | None = None
) -> None:
    """Mirror a resolution into every channel that showed the approval.
    Never raises."""
    try:
        from app.services.telegram_bot import telegram_bot

        await telegram_bot.update_resolved_telegram(
            approval_id, status, resolver_note
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram approval resolve %s failed: %s", approval_id, e)

    try:
        await _resolve_slack(approval_id, status, resolver_note)
    except Exception as e:  # noqa: BLE001
        logger.warning("slack approval resolve %s failed: %s", approval_id, e)


# ── Slack backend ─────────────────────────────────────────────────────────


async def _send_approval_slack(
    approval_id: uuid.UUID,
    agent_name: str,
    task_title: str,
    blocker_comment: str,
    tokens: tuple[str, str] | None,
) -> None:
    channel_cfg = _slack_channel()
    if not channel_cfg or not tokens:
        return

    from app.config import settings
    from app.services.slack_client import SlackTransport, resolve_channel_id

    # ⚠️ Nur chat.postMessage nimmt "#name" — alles andere braucht die ID,
    # und für die spätere ✅-Reply speichern wir Kanal+ts als Paar.
    channel = await resolve_channel_id(channel_cfg)
    if not channel:
        logger.warning("slack approvals: Kanal %r nicht auflösbar", channel_cfg)
        return

    approve_token, reject_token = tokens
    base = settings.mc_base_url.rstrip("/")
    approve_url = (
        f"{base}/api/v1/approvals/{approval_id}/quick-resolve?token={approve_token}"
    )
    reject_url = (
        f"{base}/api/v1/approvals/{approval_id}/quick-resolve?token={reject_token}"
    )

    fallback = f"Approval nötig: {agent_name} — {task_title}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Approval nötig*\n\n*Agent:* {agent_name}\n"
                    f"*Task:* {task_title}\n\n"
                    f"*Blocker:*\n{blocker_comment[:500]}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Entblocken"},
                    "style": "primary",
                    "url": approve_url,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Abbrechen"},
                    "style": "danger",
                    "url": reject_url,
                },
            ],
        },
    ]

    result = await SlackTransport().post_message(
        channel=channel, text=fallback, blocks=blocks, silent=False
    )
    if not result.ok or not result.ts:
        logger.warning("slack approval %s not delivered: %s", approval_id, result.error)
        return

    from app.redis_client import get_redis

    redis = await get_redis()
    await redis.set(
        _slack_redis_key(str(approval_id)), f"{channel}|{result.ts}", ex=_REDIS_TTL
    )
    logger.info("Approval %s sent to Slack (ts=%s)", approval_id, result.ts)


async def _resolve_slack(
    approval_id: uuid.UUID, status: str, resolver_note: str | None
) -> None:
    """✅/❌ as a thread reply on the approval message (history stays)."""
    from app.redis_client import get_redis

    redis = await get_redis()
    stored = await redis.get(_slack_redis_key(str(approval_id)))
    if not stored:
        return
    if isinstance(stored, bytes):
        stored = stored.decode()
    channel, _, ts = stored.partition("|")
    if not channel or not ts:
        return

    from app.services.slack_client import SlackTransport

    emoji = "✅" if status == "approved" else "❌"
    note_line = f"\n*Notiz:* {resolver_note}" if resolver_note else ""
    await SlackTransport().post_message(
        channel=channel,
        text=f"{emoji} Approval *{status}*{note_line}",
        thread_ts=ts,
    )
    await redis.delete(_slack_redis_key(str(approval_id)))
