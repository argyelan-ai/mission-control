"""task_state — the single, row-locked path for Task status transitions.

Today ~55 call sites across 16 files write ``task.status = X`` directly,
none of them taking a row lock first (``grep -rn with_for_update backend/app``
only turns up routers/nodes.py and services/messaging.py). Two valid
transitions racing on the same task both "win" in the ORM's in-memory view —
whichever commits last silently overwrites the other, which is how a task
gets dispatched twice. ``lock_and_set()`` is the fix: it re-reads the row
under ``SELECT ... FOR UPDATE`` (Postgres only — see the dialect guard
below, same pattern as routers/nodes.py:pair()), validates against
``task_status.VALID_TRANSITIONS`` using that freshly-locked status (not a
stale in-memory copy), and sets ``task.status``. Migration 0159's Postgres
trigger (``validate_task_transition``) stays in place as the second,
always-on net — this only closes the race between two otherwise-valid
transitions.

``lock_and_set()`` does NOT commit and does NOT emit an event — callers that
bundle the status write with other fields/rows in one atomic transaction
call it directly and commit + emit their own event themselves.
``transition()`` is the thin, committing wrapper for callers that only need
to change the status: ``lock_and_set()`` + ``session.commit()`` + exactly one
generic ``task.status_changed`` event.
"""

import uuid

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.services.activity import emit_event
from app.task_status import STATUS_LABELS, VALID_TRANSITIONS, is_valid_transition


async def lock_and_set(
    session: AsyncSession,
    task_id: uuid.UUID,
    to: str,
    *,
    actor: str,
) -> tuple[Task, str]:
    """Lock ``task_id``'s row, validate, and set ``task.status`` to ``to``.

    Does NOT commit and does NOT emit an event — the caller is responsible
    for both, together with whatever else it writes in the same
    transaction. Returns ``(task, from_status)``.

    Raises HTTPException(404) if the task doesn't exist, HTTPException(409)
    if ``to`` is not a valid transition from the task's current status.
    Callers outside a request context (e.g. the watchdog) must catch the
    409 themselves — it is a plain HTTPException, not tied to a response.

    The 409's ``detail`` is a structured dict (``current_status``,
    ``expected``, ``allowed``, plus a human-readable ``message``) — not
    prose. A caller that only sees the rendered string (e.g. the omp-bridge,
    incident #477) has no reliable way to tell "lost the lock_and_set() race
    this was built to catch" (expected, retryable) apart from a genuine
    error; the structured fields make that machine-checkable.
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
            detail={
                "error": "invalid_transition",
                "current_status": from_status,
                "expected": to,
                "allowed": sorted(VALID_TRANSITIONS.get(from_status, set())),
                "message": (
                    f"Ungueltiger Statuswechsel: "
                    f"{STATUS_LABELS.get(from_status, from_status)} -> "
                    f"{STATUS_LABELS.get(to, to)}"
                ),
            },
        )

    task.status = to
    session.add(task)

    return task, from_status


async def transition(
    session: AsyncSession,
    task_id: uuid.UUID,
    to: str,
    *,
    actor: str,
    reason: str | None = None,
) -> Task:
    """Move ``task_id`` to status ``to`` under a row lock, committing.

    Thin wrapper around ``lock_and_set()`` for the simple case: a status
    change with no other fields/rows to bundle in. Commits and emits
    exactly one ``task.status_changed`` ActivityEvent.

    Raises HTTPException(404)/HTTPException(409) — see ``lock_and_set()``.
    """
    task, from_status = await lock_and_set(session, task_id, to, actor=actor)
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
