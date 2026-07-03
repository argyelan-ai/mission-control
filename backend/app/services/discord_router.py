"""Discord Channel Router — routes events + notifications to the right channels."""

import logging

from app.redis_client import get_redis

logger = logging.getLogger("mc.discord_router")

# Redis keys for channel IDs (set by the setup script)
CHANNEL_PURPOSES = ("alerts", "reviews", "briefing", "deploy", "ideas", "github", "jobs")


async def get_channel_id(purpose: str) -> str | None:
    """Read the channel ID for a given purpose from Redis."""
    r = await get_redis()
    return await r.get(f"mc:discord:channel:{purpose}")


async def notify_user_test(task_title: str, task_id: str) -> None:
    """Task ready for testing → #mc-reviews."""
    channel_id = await get_channel_id("reviews")
    if not channel_id:
        return
    from app.services.discord import send_to_discord_channel
    await send_to_discord_channel(channel_id, embed={
        "title": f"Bereit zum Testen: {task_title}",
        "description": f"Bitte auf dem Handy testen.\nTask-ID: `{task_id}`",
        "color": 0x7C3AED,
    })


async def notify_deploy(service: str, status: str, details: str) -> None:
    """Deploy status → #deploy-log."""
    channel_id = await get_channel_id("deploy")
    if not channel_id:
        return
    from app.services.discord import send_to_discord_channel
    color = 0x00CC88 if status == "ok" else 0xEF4444
    emoji = "+" if status == "ok" else "x"
    await send_to_discord_channel(channel_id, embed={
        "title": f"{emoji} Deploy: {service}",
        "description": details,
        "color": color,
    })


async def notify_alert(title: str, description: str, severity: str = "warning") -> None:
    """Warning/error/critical → #mc-alerts."""
    channel_id = await get_channel_id("alerts")
    if not channel_id:
        return
    from app.services.discord import send_to_discord_channel
    color = {"warning": 0xFFB224, "error": 0xEF4444, "critical": 0x7C1FFF}.get(severity, 0x3B82F6)
    await send_to_discord_channel(channel_id, embed={
        "title": title,
        "description": description,
        "color": color,
    })
