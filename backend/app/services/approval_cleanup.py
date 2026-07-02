"""Approval Cleanup — Task-Status ist Source of Truth.

Wenn ein Task den Zustand verlaesst der die Approval ausgeloest hat,
wird die Approval auf 'superseded' gesetzt (nicht resolved/approved —
das ist die explizite Entscheidung des Operators).

Zwei Mechanismen:
1. Sofort-Cleanup bei Task-Statuswechsel (aufgerufen aus agent_scoped/tasks)
2. Watchdog-Reconciliation als Sicherheitsnetz (periodisch)
"""

import logging
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.approval_cleanup")

# Wann ist welcher Approval-Typ obsolet?
# Key: action_type -> Value: Set von Task-Status bei denen die Approval NOCH gueltig ist
APPROVAL_VALID_STATES: dict[str, set[str]] = {
    "blocker_decision": {"blocked"},
    "spawn_timeout": {"inbox"},
    "dispatch_escalation": {"inbox"},
}


async def cleanup_obsolete_approvals(
    session: AsyncSession,
    task_id: uuid.UUID,
    new_status: str,
    board_id: uuid.UUID | None = None,
) -> int:
    """Sofort-Cleanup: Offene Approvals superseden wenn Task-Status sie obsolet macht."""
    result = await session.exec(
        select(Approval).where(
            Approval.task_id == task_id,
            Approval.status == "pending",
        )
    )
    pending = result.all()

    if not pending:
        return 0

    superseded_count = 0
    now = utcnow()

    for approval in pending:
        valid_states = APPROVAL_VALID_STATES.get(approval.action_type)
        if valid_states is None:
            continue

        if new_status not in valid_states:
            approval.status = "superseded"
            approval.resolved_at = now
            approval.resolver_note = f"Superseded: Task wechselte auf '{new_status}'"
            session.add(approval)
            superseded_count += 1

            logger.info(
                "Approval superseded: %s fuer Task %s (Task jetzt '%s')",
                approval.action_type, task_id, new_status,
            )

            if board_id:
                await emit_event(
                    session, "approval.superseded",
                    f"Approval '{approval.action_type}' automatisch geschlossen (Task nicht mehr {', '.join(valid_states)})",
                    board_id=board_id, task_id=task_id, agent_id=approval.agent_id,
                    detail={"approval_id": str(approval.id), "action_type": approval.action_type, "new_status": new_status},
                )

    if superseded_count:
        await session.commit()

    return superseded_count


async def reconcile_stale_approvals(session: AsyncSession) -> int:
    """Watchdog-Reconciliation: Alle pending Approvals pruefen und obsolete superseden."""
    from app.models.task import Task

    result = await session.exec(
        select(Approval).where(Approval.status == "pending")
    )
    pending = result.all()

    if not pending:
        return 0

    superseded_count = 0
    now = utcnow()

    for approval in pending:
        if not approval.task_id:
            continue

        valid_states = APPROVAL_VALID_STATES.get(approval.action_type)
        if valid_states is None:
            continue

        task = await session.get(Task, approval.task_id)
        if not task:
            approval.status = "superseded"
            approval.resolved_at = now
            approval.resolver_note = "Superseded: Task existiert nicht mehr"
            session.add(approval)
            superseded_count += 1
            continue

        if task.status not in valid_states:
            approval.status = "superseded"
            approval.resolved_at = now
            approval.resolver_note = f"Superseded (reconciliation): Task ist '{task.status}'"
            session.add(approval)
            superseded_count += 1

            logger.info(
                "Reconciliation: Approval %s superseded fuer Task '%s' (status=%s)",
                approval.action_type, task.title, task.status,
            )

    if superseded_count:
        await session.commit()
        logger.info("Reconciliation: %d stale approval(s) superseded", superseded_count)

    return superseded_count
