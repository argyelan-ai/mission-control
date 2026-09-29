"""
Discord notifications — webhook (ops alerts) + Bot API (channel-specific messages).
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger("mc.discord")

SEVERITY_COLORS = {
    "warning": 0xFFB224,
    "error": 0xEF4444,
    "critical": 0x7C1FFF,
}

def _bot_headers() -> dict[str, str]:
    bot_token = settings.discord_bot_token
    if not bot_token:
        raise RuntimeError("Discord bot token is not configured")
    return {"Authorization": f"Bot {bot_token}"}


async def send_discord_notification(
    title: str,
    description: str,
    severity: str = "warning",
    fields: list[dict] | None = None,
) -> None:
    """Ops webhook for warning/error/critical events."""
    webhook_url = settings.discord_webhook_ops
    if not webhook_url:
        return

    color = SEVERITY_COLORS.get(severity, 0x3B82F6)
    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
    }
    if fields:
        embed["fields"] = fields

    payload = {"embeds": [embed]}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(webhook_url, json=payload)
    except Exception:
        pass  # Notifications are best-effort — never crash the main flow


async def send_to_discord_channel(
    channel_id: str,
    content: str | None = None,
    embed: dict | None = None,
) -> None:
    """Post a message to a specific Discord channel via bot token."""
    bot_token = settings.discord_bot_token
    if not bot_token:
        return

    payload: dict = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]

    if not payload:
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                json=payload,
                headers=_bot_headers(),
            )
            if resp.status_code >= 400:
                logger.warning("Discord Bot API error %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.debug("Discord channel message failed: %s", e)
