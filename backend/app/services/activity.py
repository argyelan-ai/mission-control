"""
Helper to create ActivityEvent rows and publish them to Redis for SSE fan-out.
"""

import logging
import uuid
from datetime import datetime

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.redis_client import RedisKeys
from app.services.discord import send_discord_notification
from app.services.sse import broadcast
from app.utils import utcnow


async def emit_event(
    session: AsyncSession,
    event_type: str,
    title: str,
    severity: str = "info",
    board_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> ActivityEvent:
    event = ActivityEvent(
        event_type=event_type,
        title=title,
        severity=severity,
        board_id=board_id,
        task_id=task_id,
        agent_id=agent_id,
        project_id=project_id,
        detail=detail,
        created_at=utcnow(),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    # Publish to Redis channels for SSE fan-out
    event_data = {
        "id": str(event.id),
        "event_type": event_type,
        "title": title,
        "severity": severity,
        "board_id": str(board_id) if board_id else None,
        "task_id": str(task_id) if task_id else None,
        "agent_id": str(agent_id) if agent_id else None,
        "project_id": str(project_id) if project_id else None,
        "detail": detail,
        "created_at": event.created_at.isoformat(),
    }

    # Global activity channel
    await broadcast(RedisKeys.activity_events(), event_type, event_data)

    # Board-specific channel
    if board_id:
        await broadcast(RedisKeys.board_events(str(board_id)), event_type, event_data)

    # Agent-specific channel
    if agent_id and event_type.startswith("agent."):
        await broadcast(RedisKeys.agents_events(), event_type, event_data)

    if event_type.startswith("approval."):
        await broadcast(RedisKeys.approvals_events(), event_type, event_data)

    # Discord push for warning+
    if severity in ("warning", "error", "critical"):
        await send_discord_notification(
            title=title,
            description=f"Event: `{event_type}`",
            severity=severity,
        )
        # Auch an #mc-alerts Channel
        try:
            from app.services.discord_router import notify_alert
            await notify_alert(title, f"Event: `{event_type}`", severity)
        except Exception:
            pass

    # Agent-spezifische Events an Agent-Discord-Channel routen
    if agent_id and event_type.startswith(("task.", "agent.")):
        try:
            from app.services.discord import send_to_discord_channel
            from sqlmodel import select
            from app.models.agent import Agent
            agent_result = await session.exec(
                select(Agent.discord_channel_id).where(Agent.id == agent_id)
            )
            agent_channel = agent_result.first()
            if agent_channel:
                await send_to_discord_channel(str(agent_channel), embed={
                    "title": title,
                    "description": f"`{event_type}`",
                    "color": 0x3B82F6,
                })
        except Exception:
            pass  # Best-effort

    # user_test Status → #mc-reviews
    if (event_type == "task.status_changed" and detail
            and detail.get("new_status") == "user_test"):
        try:
            from app.services.discord_router import notify_user_test
            await notify_user_test(title, str(task_id) if task_id else "")
        except Exception:
            pass

    return event


logger = logging.getLogger("mc.activity")
