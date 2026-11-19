"""Who delivered a task — read from its status history, not stored.

The hand-off to review clears ``assigned_agent_id`` (operator review) or
moves it to the reviewer (agent review) on purpose: the inbox and the review
routing read "nobody assigned" as "the operator decides". The agent that
built the card was lost with it — the task said "Unassigned / not
dispatched" and the agent filter dropped the card (round-2 user test,
t1 R1-SHOULD-4 / t4 S-neu-4).

The task core is frozen (PRINCIPLES: no new task columns), so the deliverer
is derived: the agent of the latest ``task.status_changed`` activity event
that moved the card ``in_progress -> review``. The same signal
``work_context.find_last_developer`` uses first. A move by the operator
(no agent on the event) names nobody — the card then shows no deliverer,
as before.

Read-only; the task list and the single-task read send it along as
``delivered_by_agent_id`` / ``delivered_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Iterable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.task import Task

# A card has been delivered once it reached one of these. Earlier states
# have nobody to name; failed cards are not a delivery.
DELIVERED_STATUSES = frozenset({"review", "user_test", "done"})

# Keeps the IN (...) list under SQLite's bound-parameter limit.
_CHUNK = 400


async def find_deliverers(
    session: AsyncSession, tasks: Iterable[Task]
) -> dict[uuid.UUID, tuple[uuid.UUID, datetime]]:
    """task id -> (agent id, when) of the latest agent hand-off to review."""
    ids = [t.id for t in tasks if t.status in DELIVERED_STATUSES]
    found: dict[uuid.UUID, tuple[uuid.UUID, datetime]] = {}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        rows = await session.exec(
            select(ActivityEvent.task_id, ActivityEvent.agent_id, ActivityEvent.created_at)
            .where(
                ActivityEvent.task_id.in_(chunk),  # type: ignore[union-attr]
                ActivityEvent.event_type == "task.status_changed",
                ActivityEvent.agent_id.is_not(None),  # type: ignore[union-attr]
                ActivityEvent.detail["old_status"].as_string() == "in_progress",  # type: ignore[index]
                ActivityEvent.detail["new_status"].as_string() == "review",  # type: ignore[index]
            )
            .order_by(ActivityEvent.created_at.desc())  # type: ignore[union-attr]
        )
        for task_id, agent_id, created_at in rows.all():
            found.setdefault(task_id, (agent_id, created_at))
    return found


def delivery_fields(
    found: dict[uuid.UUID, tuple[uuid.UUID, datetime]], task_id: uuid.UUID
) -> dict[str, str | None]:
    """The two response fields for one task (``None`` when unknown)."""
    hit = found.get(task_id)
    if not hit:
        return {"delivered_by_agent_id": None, "delivered_at": None}
    agent_id, at = hit
    return {"delivered_by_agent_id": str(agent_id), "delivered_at": at.isoformat() if at else None}
