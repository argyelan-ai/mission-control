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

TEXT_CHANNEL_TYPES = {0, 5}


def _bot_headers() -> dict[str, str]:
    bot_token = settings.discord_bot_token
    if not bot_token:
        raise RuntimeError("Discord bot token is not configured")
    return {"Authorization": f"Bot {bot_token}"}


async def list_guild_channels(guild_id: str) -> list[dict]:
    """List text-like channels for a Discord guild via Bot API."""
    async with httpx.AsyncClient(timeout=15.0, headers=_bot_headers(), base_url="https://discord.com/api/v10") as client:
        resp = await client.get(f"/guilds/{guild_id}/channels")
        resp.raise_for_status()
        channels = resp.json()

    categories = {
        str(channel["id"]): str(channel.get("name") or "")
        for channel in channels
        if int(channel.get("type", -1)) == 4
    }

    result: list[dict] = []
    for channel in channels:
        channel_type = int(channel.get("type", -1))
        if channel_type not in TEXT_CHANNEL_TYPES:
            continue

        parent_name = categories.get(str(channel.get("parent_id") or ""), "")
        topic = str(channel.get("topic") or "").strip()
        context = topic or parent_name or "Discord channel"
        result.append(
            {
                "id": str(channel["id"]),
                "name": str(channel.get("name") or ""),
                "context": context,
                "bound_agent_id": None,
                "parent_name": parent_name or None,
                "channel_type": channel_type,
                "position": int(channel.get("position") or 0),
            }
        )

    result.sort(key=lambda item: ((item.get("parent_name") or "").lower(), item["position"], item["name"].lower()))
    return result


async def create_guild_text_channel(
    guild_id: str,
    *,
    name: str,
    context: str,
    category_id: str | None = None,
) -> dict:
    """Create a text channel in a Discord guild via Bot API."""
    payload: dict[str, object] = {
        "name": name,
        "type": 0,
    }
    if category_id:
        payload["parent_id"] = category_id
    topic = context.strip()
    if topic:
        payload["topic"] = topic[:1024]

    async with httpx.AsyncClient(timeout=15.0, headers=_bot_headers(), base_url="https://discord.com/api/v10") as client:
        resp = await client.post(f"/guilds/{guild_id}/channels", json=payload)
        resp.raise_for_status()
        channel = resp.json()

    return {
        "id": str(channel["id"]),
        "name": str(channel.get("name") or name),
        "context": topic or "Discord channel",
        "bound_agent_id": None,
    }


async def send_discord_notification(
    title: str,
    description: str,
    severity: str = "warning",
    fields: list[dict] | None = None,
) -> None:
    """Ops-Webhook fuer warning/error/critical Events."""
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
    """Nachricht in einen spezifischen Discord-Channel posten via Bot Token."""
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


# ---------------------------------------------------------------------------
# Phase 30 — discord_config (single-row) read helper
# ---------------------------------------------------------------------------

from app.models.discord_config import DiscordConfig  # noqa: E402
from sqlmodel import select  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402


async def get_discord_config(session: AsyncSession) -> DiscordConfig | None:
    """Read the single discord_config row. Application-enforced single-row.

    Returns None if no row exists yet (Plan 30-03 migration seeds it; before
    that, defensive fallback that lets the router serve an empty config
    response without 500-ing).
    """
    result = await session.exec(select(DiscordConfig).limit(1))
    return result.first()
