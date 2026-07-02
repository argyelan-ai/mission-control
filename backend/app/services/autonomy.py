"""
Autonomy Service — 3-Tier Autonomy Levels.

L1 = Auto (execute immediately, no approval needed)
L2 = Notify (execute + emit activity event for visibility)
L3 = Approve (create approval, wait for the operator's decision)

Defaults per action_type can be overridden via Redis.
"""

import json
import logging

from app.redis_client import get_redis
from app.services.activity import emit_event

logger = logging.getLogger("mc.autonomy")

AUTONOMY_DEFAULTS: dict[str, str] = {
    "deploy": "L3",
    "external_post": "L3",
    "config_change": "L3",
    "browser_action": "L2",
    "visual_review": "L3",
    "blocker_decision": "L3",
    "question": "L3",
    "code_change": "L1",
    "mark_done": "L1",
    "dispatch_escalation": "L3",
    "recovery_failed": "L3",
}

REDIS_KEY = "mc:settings:autonomy"


async def get_autonomy_config() -> dict[str, str]:
    """Get current autonomy levels (Redis overrides + defaults)."""
    try:
        redis = await get_redis()
        raw = await redis.get(REDIS_KEY)
        if raw:
            overrides = json.loads(raw)
            return {**AUTONOMY_DEFAULTS, **overrides}
    except Exception:
        pass
    return dict(AUTONOMY_DEFAULTS)


async def set_autonomy_config(overrides: dict[str, str]) -> dict[str, str]:
    """Save autonomy level overrides to Redis."""
    valid_levels = {"L1", "L2", "L3"}
    for action, level in overrides.items():
        if level not in valid_levels:
            raise ValueError(f"Invalid level '{level}' for '{action}'. Must be L1, L2, or L3.")
    redis = await get_redis()
    await redis.set(REDIS_KEY, json.dumps(overrides))
    return {**AUTONOMY_DEFAULTS, **overrides}


async def resolve_autonomy(action_type: str) -> str:
    """Returns 'L1' | 'L2' | 'L3' for a given action_type."""
    config = await get_autonomy_config()
    return config.get(action_type, "L3")  # Default to L3 (safest)


async def enforce_autonomy(
    action_type: str,
    session,
    agent_id,
    board_id,
    description: str,
    task_id=None,
    payload=None,
    confidence=None,
) -> str:
    """
    Enforce autonomy level for an action.

    Returns the autonomy level that was applied:
    - L1: Action proceeds, nothing logged
    - L2: Action proceeds, activity event emitted
    - L3: Approval created, action blocked until resolved
    """
    level = await resolve_autonomy(action_type)

    if level == "L1":
        return "L1"

    if level == "L2":
        await emit_event(
            session,
            f"autonomy.l2.{action_type}",
            f"[L2 Notify] {description}",
            board_id=board_id,
            agent_id=agent_id,
            task_id=task_id,
            severity="info",
        )
        return "L2"

    # L3: Create approval
    from app.models.approval import Approval
    approval = Approval(
        board_id=board_id,
        task_id=task_id,
        agent_id=agent_id,
        action_type=action_type,
        description=description,
        payload=payload,
        confidence=confidence,
        autonomy_level="L3",
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)

    await emit_event(
        session,
        "approval.created",
        f"[L3 Approve] {description}",
        board_id=board_id,
        agent_id=agent_id,
        task_id=task_id,
        severity="warning",
    )

    return "L3"
