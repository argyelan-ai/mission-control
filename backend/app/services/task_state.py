"""task_state — the row-locked path for Task status transitions.

Today ~55 call sites across 16 files write ``task.status = X`` directly,
none of them taking a row lock first (``grep -rn with_for_update backend/app``
only turns up routers/nodes.py and services/messaging.py). Two valid
transitions racing on the same task both "win" in the ORM's in-memory view —
whichever commits last silently overwrites the other, which is how a task
gets dispatched twice. ``lock_and_set()`` is the fix: it re-reads the row
under ``SELECT ... FOR UPDATE`` (Postgres only — see the dialect guard
below, same pattern as routers/nodes.py:pair()) with
``execution_options(populate_existing=True)``, validates against
``task_status.VALID_TRANSITIONS`` using that freshly-locked status (not a
stale in-memory copy — SQLAlchemy's identity map returns an already-attached
object unchanged on a plain ``select()``, ``populate_existing`` is what forces
the refresh, see PR #478 review finding B1), and sets ``task.status``.
Migration 0159's Postgres trigger (``validate_task_transition``) stays in
place as the second, always-on net — this only closes the race between two
otherwise-valid transitions.

``lock_and_set()`` does NOT commit and does NOT emit an event — callers that
bundle the status write with other fields/rows in one atomic transaction
call it directly and commit + emit their own event themselves.
``transition()`` is the thin, committing wrapper for callers that only need
to change the status: ``lock_and_set()`` + ``session.commit()`` + exactly one
generic ``task.status_changed`` event. ``lock_task()`` is the read-only half
for callers whose transition rules ``is_valid_transition()`` can't express
(see its docstring).

Scope, as of this PR (#478 review, B2): ``lock_task()``/``lock_and_set()``
now also back the two highest-traffic status writers — the operator PATCH
(``routers/tasks.py:update_task``) and the agent PATCH behind
``mc ack``/``mc review``/``mc done``/``mc finish``
(``routers/agent_task_status.py:agent_update_task``) — closing the from_status
validation against a stale in-memory copy for both. Neither endpoint holds
that lock for its *entire* request, though: both have legitimate mid-request
commits before the final status write (approval-cleanup / blocker-approval
resolution in ``agent_update_task``, the report-back auto-draft's own atomic
CAS commit). Postgres releases ``FOR UPDATE`` at commit, so the window between
such an intermediate commit and the final write is not lock-protected — each
of those commits is followed by ``session.refresh(task)``, so the object
itself is never stale there, but two concurrent requests could still both
pass validation in that narrow window. Fully closing that would mean
re-acquiring the lock after every intermediate commit, which is a larger,
separate change against those endpoints' existing atomicity guarantees and is
not attempted here. The remaining ~20 call sites (seed/bootstrap paths,
``task_context_builder``, single-writer watchdog sweeps already covered by
their own guards) are unconverted for the reasons noted at each site.
"""

import logging
import uuid

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.services.activity import emit_event
from app.task_status import STATUS_LABELS, VALID_TRANSITIONS, is_valid_transition

logger = logging.getLogger("mc.task_state")


async def lock_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Lock and freshly re-read ``task_id``'s row — the read half of
    ``lock_and_set()``, without the ``VALID_TRANSITIONS`` check or the
    status write.

    For callers that carry their own transition/business-rule validation
    that ``lock_and_set()``'s strict ``is_valid_transition()`` can't express
    (e.g. ``routers/tasks.py:_enforce_board_rules``'s operator-widened
    ``failed -> done/aborted`` closeout, or ``agent_task_status.py``'s
    reviewer/blocker-approval guards). Those callers must re-fetch the task
    through here — instead of trusting whatever they loaded earlier in the
    same session for an unrelated 404/ownership check — before reading
    ``task.status`` for validation, for exactly the reason B1 in the PR
    #478 review exists: SQLAlchemy's identity map returns an
    already-attached object unchanged on a plain ``select()``, so a plain
    re-select after a concurrent writer's commit still hands back the
    stale copy. ``execution_options(populate_existing=True)`` is what
    forces the refresh; see ``lock_and_set()`` below for the same fix.

    Returns ``None`` if the task doesn't exist (caller raises its own 404 —
    this helper doesn't know the caller's error format).
    """
    stmt = select(Task).where(Task.id == task_id).execution_options(
        populate_existing=True
    )
    if session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return (await session.exec(stmt)).first()


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
    task = await lock_task(session, task_id)
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

    logger.debug(
        "task_state.lock_and_set: task=%s %s->%s actor=%s",
        task_id,
        from_status,
        to,
        actor,
    )

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
