"""task_state — the single, row-locked path for Task status transitions.

Today ~55 call sites across 16 files write ``task.status = X`` directly,
none of them taking a row lock first (``grep -rn with_for_update backend/app``
only turns up routers/nodes.py and services/messaging.py). Two valid
transitions racing on the same task both "win" in the ORM's in-memory view —
whichever commits last silently overwrites the other, which is how a task
gets dispatched twice. ``transition()`` is the fix: it re-reads the row
under ``SELECT ... FOR UPDATE`` (Postgres only — see the dialect guard
below, same pattern as routers/nodes.py:pair()), validates against
``task_status.VALID_TRANSITIONS`` using that freshly-locked status (not a
stale in-memory copy), and emits exactly one ``task.status_changed``
ActivityEvent. Migration 0159's Postgres trigger (``validate_task_transition``)
stays in place as the second, always-on net — this only closes the race
between two otherwise-valid transitions.
"""

import uuid

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.services.activity import emit_event
from app.task_status import STATUS_LABELS, is_valid_transition


async def transition(
    session: AsyncSession,
    task_id: uuid.UUID,
    to: str,
    *,
    actor: str,
    reason: str | None = None,
) -> Task:
    """Move ``task_id`` to status ``to`` under a row lock.

    Raises HTTPException(404) if the task doesn't exist, HTTPException(409)
    if ``to`` is not a valid transition from the task's current status.
    Callers outside a request context (e.g. the watchdog) must catch the
    409 themselves — it is a plain HTTPException, not tied to a response.
    """
    stmt = select(Task).where(Task.id == task_id)
    if session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    task = (await session.exec(stmt)).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Task nicht gefunden")

    from_status = task.status
    if not is_valid_transition(from_status, to):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ungueltiger Statuswechsel: "
                f"{STATUS_LABELS.get(from_status, from_status)} -> "
                f"{STATUS_LABELS.get(to, to)}"
            ),
        )

    task.status = to
    session.add(task)
    await session.commit()
    await session.refresh(task)

    await emit_event(
        session,
        "task.status_changed",
        title=(
            f"{STATUS_LABELS.get(from_status, from_status)} -> "
            f"{STATUS_LABELS.get(to, to)}"
        ),
        board_id=task.board_id,
        task_id=task.id,
        detail={"from": from_status, "to": to, "actor": actor, "reason": reason},
    )

    return task
