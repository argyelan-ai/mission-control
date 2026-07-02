"""Single source of truth for writes to tasks.dispatch_attempt_id.

Why: the 2026-05-15 doppelter-dispatch bug (Researcher / Wetter-Staufen)
showed that 12 separate call sites mutated this field directly. When the
field rotated unexpectedly mid-dispatch, forensics had no audit trail —
30min of code archaeology yielded only a hypothesis. From now on every
write to `task.dispatch_attempt_id` MUST go through:

    set_dispatch_attempt_id(session, task, new_id, *, caller, reason)
    clear_dispatch_attempt_id(session, task, *, caller, reason)

Both helpers log a structured line AND insert a `TaskAttemptAudit` row in
the same transaction. The next similar incident is one SQL query away
instead of a 30-minute investigation.

Set has a `only_if_null` mode that uses a conditional UPDATE …
WHERE dispatch_attempt_id IS NULL — race-safe when two paths (e.g.
auto_dispatch_task vs /agent/me/poll during a 5s git-clone window) both
try to initialise the id. Only the first writer wins, the loser sees
False and the existing id remains canonical.
"""
import logging
import uuid

from sqlalchemy import update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.models.task_attempt_audit import TaskAttemptAudit

logger = logging.getLogger("mc.dispatch_attempt_audit")


def _parse_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def set_dispatch_attempt_id(
    session: AsyncSession,
    task: Task,
    new_id: str | uuid.UUID,
    *,
    caller: str,
    reason: str | None = None,
    only_if_null: bool = False,
) -> bool:
    """Set task.dispatch_attempt_id to ``new_id`` with audit trail.

    Args:
        session: open AsyncSession; helper commits exactly once at the end.
        task: ORM instance — its in-memory state is refreshed before return.
        new_id: target UUID (str or uuid.UUID).
        caller: short identifier of the code path performing the write.
            Stored verbatim in the audit row.
        reason: free-form context (e.g. "race_winner", "ack_timeout_half").
        only_if_null: when True, the UPDATE is conditional on the current
            dispatch_attempt_id being NULL. Used at the two race-prone
            initialisation sites (auto_dispatch_task, /agent/me/poll) to
            ensure first-writer-wins instead of last-commit-wins.

    Returns:
        True  if the write happened (audit row inserted, task.dispatch_attempt_id == new_id).
        False if ``only_if_null`` was True and another transaction already
              set the id — task is refreshed from DB so caller sees the
              canonical value. No audit row is written in that case.
    """
    new_uuid_str = str(new_id)
    old_id = task.dispatch_attempt_id

    if only_if_null:
        stmt = (
            update(Task)
            .where(Task.id == task.id, Task.dispatch_attempt_id.is_(None))
            .values(dispatch_attempt_id=new_uuid_str)
        )
        result = await session.exec(stmt)  # type: ignore[arg-type]
        rowcount = getattr(result, "rowcount", None)
        if rowcount is None or rowcount == 0:
            # Lost the race — re-sync ORM state from DB so caller respects
            # the canonical winner.
            await session.refresh(task)
            logger.info(
                "attempt_id_change_lost task=%s caller=%s reason=%s "
                "winner=%s requested=%s",
                task.id, caller, reason, task.dispatch_attempt_id, new_uuid_str,
            )
            return False
        # Conditional UPDATE landed — sync ORM in-memory.
        task.dispatch_attempt_id = new_uuid_str
    else:
        task.dispatch_attempt_id = new_uuid_str
        session.add(task)

    audit = TaskAttemptAudit(
        task_id=task.id,
        old_attempt=_parse_uuid(old_id),
        new_attempt=_parse_uuid(new_uuid_str),
        caller=caller,
        reason=reason,
    )
    session.add(audit)
    await session.commit()
    await session.refresh(task)

    logger.info(
        "attempt_id_change task=%s caller=%s reason=%s old=%s new=%s",
        task.id, caller, reason, old_id, new_uuid_str,
    )
    return True


async def clear_dispatch_attempt_id(
    session: AsyncSession,
    task: Task,
    *,
    caller: str,
    reason: str | None = None,
) -> None:
    """Clear task.dispatch_attempt_id (set to NULL) with audit trail.

    No-op if already NULL — no audit row is written for a transition that
    did not happen.
    """
    old_id = task.dispatch_attempt_id
    if old_id is None:
        return

    task.dispatch_attempt_id = None
    session.add(task)

    audit = TaskAttemptAudit(
        task_id=task.id,
        old_attempt=_parse_uuid(old_id),
        new_attempt=None,
        caller=caller,
        reason=reason,
    )
    session.add(audit)
    await session.commit()
    await session.refresh(task)

    logger.info(
        "attempt_id_clear task=%s caller=%s reason=%s old=%s",
        task.id, caller, reason, old_id,
    )
