"""Receiver for the ``task.stuck`` escalation event.

Hole 3 of the 2026-09-18 watchdog-blindness analysis (card b2ea802f sat 9h
in in_progress): ``task.stuck`` was written by the stale-check circuit
breaker (task_runner._check_stale_in_progress) but nothing consumed it —
only a UI badge mapping (routers/tasks.py) and an agent pull endpoint
(/me/activity-events) ever read it. It was a log entry, not an alarm.

This module IS the receiver: it turns the event into the same
lead-visible alarm the silent-card watchdog uses (a ``watchdog_notify``
comment on the card, addressed to the Board Lead). That channel is proven
— it is the one watchdog report that worked that night — and it needs no
external config (the Discord path silently no-ops without a webhook, and
the ``Notification`` model has no writer at all).

Routing lives in services/activity.py::emit_event (the one alarm door) so
every future ``task.stuck`` emitter gets the receiver for free.
"""

from __future__ import annotations

import logging

from datetime import timedelta
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.activity import ActivityEvent
from app.models.task import Task, TaskComment
from app.utils import ensure_aware, utcnow

logger = logging.getLogger("mc.stuck_escalation")

#: If ANY watchdog_notify landed on the card within this window, a second
#: alarm would be noise, not signal — the card already has a fresh report
#: on its thread. The stale-check's own Redis escalation key (TTL 86400)
#: bounds ``task.stuck`` to ~1/day per task; this guard only protects
#: against a future second emitter double-alarming an already-reported card.
STUCK_RELAY_DEDUP_MINUTES = 60


async def relay_stuck_event(session: AsyncSession, event: ActivityEvent) -> None:
    """Post the stuck escalation as a lead-visible watchdog_notify comment.
    Best-effort by contract: the caller wraps this in try/except — a relay
    failure must never break the emit that triggered it.
    """
    if event.task_id is None:
        return
    task = await session.get(Task, event.task_id)
    if task is None:
        return

    cutoff = utcnow() - timedelta(minutes=STUCK_RELAY_DEDUP_MINUTES)
    recent_notify = (await session.exec(
        select(TaskComment)
        .where(
            TaskComment.task_id == task.id,
            TaskComment.comment_type == "watchdog_notify",
            TaskComment.created_at >= cutoff,  # type: ignore[union-attr]
        )
        .limit(1)
    )).first()
    if recent_notify is not None:
        logger.debug(
            "task.stuck relay skipped for '%s' — watchdog_notify < %dmin old",
            (task.title or "")[:60], STUCK_RELAY_DEDUP_MINUTES,
        )
        return

    lead = None
    if task.board_id:
        lead = (await session.exec(
            select(Agent).where(
                Agent.board_id == task.board_id,
                Agent.is_board_lead == True,  # noqa: E712
            )
        )).first()

    detail = event.detail or {}
    minutes = int(detail.get("minutes_since_activity") or 0)
    checks = int(detail.get("check_count") or 0)
    assignee = detail.get("agent_name") or "unbekannt"

    if lead is not None:
        addressee = f"**Board-Lead {lead.name}:** bitte pruefen und dem Agenten folgen."
    else:
        addressee = "**Operator:** keine Board-Lead auf diesem Board — bitte pruefen."

    msg = (
        f"STUCK-ESKALATION: \"{task.title}\" bei {assignee} — {checks}x "
        f"Status-Check ohne Fortschritt, letzte Aktivitaet vor {minutes}min. "
        f"Der Stale-Check hat seine Eskalationsstufe erreicht "
        f"(Circuit Breaker); automatische Recovery hat nicht gegriffen.\n\n"
        f"{addressee}\n\n"
        f"Task-ID: {task.id}"
    )
    session.add(TaskComment(
        task_id=task.id,
        author_type="system",
        content=msg,
        comment_type="watchdog_notify",
    ))
    await session.commit()
    logger.info(
        "task.stuck relayed to card thread: '%s' (lead=%s)",
        (task.title or "")[:60], lead.name if lead else None,
    )
