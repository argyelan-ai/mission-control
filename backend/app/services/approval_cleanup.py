"""Approval Cleanup — task status is the source of truth.

When a task leaves the state that triggered the approval, the
approval is set to 'superseded' (not resolved/approved — that is
the operator's explicit decision).

Two mechanisms:
1. Immediate cleanup on task status change (called from agent_scoped/tasks)
2. Watchdog reconciliation as a safety net (periodic)

Special case ``lead_escalation``: closed on card close (done/archived) via
LEAD_ESCALATION_CLOSE_ON_STATUS — see there. Deliberately NOT routed through
APPROVAL_VALID_STATES: a mere status flip (review/blocked/...) must not close
the escalation — that is the evidence-based retract path's job
(task_monitor._check_silent_card_retractions).
"""

import logging
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.approval import Approval
from app.services.activity import emit_event
from app.utils import utcnow

logger = logging.getLogger("mc.approval_cleanup")

# When does which approval type become obsolete?
# Key: action_type -> Value: set of task statuses for which the approval is STILL valid
APPROVAL_VALID_STATES: dict[str, set[str]] = {
    "blocker_decision": {"blocked"},
    # Klaerungsfrage: Task verlaesst blocked (z.B. Lead beantwortet + entblockt)
    # → die Frage ist beantwortet oder obsolet, das Approval darf nicht als
    # Zombie in der Operator-Inbox haengen bleiben.
    "clarification_question": {"blocked"},
    "spawn_timeout": {"inbox"},
    "dispatch_escalation": {"inbox"},
    # review_stuck: watchdog creates this when a review hangs (see
    # task_monitor._check_review_tasks). Once the card leaves "review"
    # (approved, changes_requested resolved, or resolved by hand), the
    # approval is moot — supersede it instead of leaving it in the
    # operator's inbox for a card that's already done.
    "review_stuck": {"review"},
    # dependency_zombie: the watchdog creates this while the card is actively
    # waiting on a dead dependency (task in inbox/in_progress). Once the card
    # leaves those states — review, done, or the dependency resolved by hand —
    # the approval is moot. Without this entry the type has NO retract path at
    # all: neither the immediate cleanup nor the reconciliation touches an
    # unknown action_type (`.get(...) is None → continue`), so a stale
    # dependency_zombie approval sits in the operator's inbox forever.
    "dependency_zombie": {"inbox", "in_progress"},
    # lead_escalation is deliberately NOT a key here — see the module
    # docstring: it has its own auto-close mechanism
    # (LEAD_ESCALATION_CLOSE_ON_STATUS / _lead_escalation_close_note) and
    # must not supersede on a mere status flip.
}

# Watchdog-generated STOERUNGSMELDUNGEN, not operator decisions: 77 of 92
# approvals in 30 days went unanswered because three of these four types
# auto-superseded (see APPROVAL_VALID_STATES above) the moment the card
# leaves the triggering state — lead_escalation instead auto-closes via
# LEAD_ESCALATION_CLOSE_ON_STATUS (see module docstring), deliberately NOT
# a key in APPROVAL_VALID_STATES. Behind
# settings.notice_only_escalations_enabled all four raise a passive notice
# instead of an Approval yes/no question.
NOTICE_ONLY_ACTION_TYPES: frozenset[str] = frozenset(
    {"dispatch_escalation", "lead_escalation", "review_stuck", "dependency_zombie"}
)

# lead_escalation (silent-card watchdog, stage 2): a CLOSED card has no open
# problem by definition, so its pending escalation closes with the card —
# but only on done/archived. failed is deliberately NOT in this set: a card
# closed with the problem still standing is exactly where an unanswered lead
# question must remain visible in the operator's inbox. Treating all
# closures alike would silently erase escalations whose problem is live.
# The note ("closed-by-close (<status>)") marks these as closed-by-close so a
# reader can tell them apart from an operator-answered escalation; the alert
# comment on the card stays.
LEAD_ESCALATION_CLOSE_ON_STATUS: frozenset[str] = frozenset({"done", "archived"})


def _lead_escalation_close_note(action_type: str, status: str) -> str | None:
    """Resolver note when a lead_escalation closes with its card, else None."""
    if action_type != "lead_escalation":
        return None
    if status not in LEAD_ESCALATION_CLOSE_ON_STATUS:
        return None
    return f"closed-by-close ({status})"


async def cleanup_obsolete_approvals(
    session: AsyncSession,
    task_id: uuid.UUID,
    new_status: str,
    board_id: uuid.UUID | None = None,
) -> int:
    """Immediate cleanup: supersede open approvals when task status makes them obsolete."""
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
        close_note = _lead_escalation_close_note(
            approval.action_type, new_status
        )
        if close_note is not None:
            approval.status = "superseded"
            approval.resolved_at = now
            approval.resolver_note = close_note
            session.add(approval)
            superseded_count += 1

            logger.info(
                "Approval superseded (card closed): %s fuer Task %s (Task jetzt '%s')",
                approval.action_type, task_id, new_status,
            )

            if board_id:
                await emit_event(
                    session, "approval.superseded",
                    f"Approval '{approval.action_type}' geschlossen (Karte '{new_status}')",
                    board_id=board_id, task_id=task_id, agent_id=approval.agent_id,
                    detail={"approval_id": str(approval.id), "action_type": approval.action_type, "new_status": new_status},
                )
            continue

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
    """Watchdog reconciliation: check all pending approvals and supersede obsolete ones."""
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

        task = await session.get(Task, approval.task_id)
        if not task:
            approval.status = "superseded"
            approval.resolved_at = now
            approval.resolver_note = "Superseded: Task existiert nicht mehr"
            session.add(approval)
            superseded_count += 1
            continue

        close_note = _lead_escalation_close_note(approval.action_type, task.status)
        if close_note is not None:
            approval.status = "superseded"
            approval.resolved_at = now
            approval.resolver_note = close_note
            session.add(approval)
            superseded_count += 1

            logger.info(
                "Reconciliation: Approval %s superseded (Karte '%s') fuer Task '%s'",
                approval.action_type, task.status, task.title,
            )
            continue

        valid_states = APPROVAL_VALID_STATES.get(approval.action_type)
        if valid_states is None:
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
