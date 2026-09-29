"""Review-park grace window (Lauf 7).

Live incident (30-day sample): a card in status `review` parked the
ASSIGNED agent unconditionally, forever. 15x the real reviewer sat parked
on a review card while other work waited (~1900 min total, 8 of those
already commented, only the approve/reject decision was outstanding);
separately 30x the DEVELOPER's own submitted card (no reviewer found,
`handle_review_handoff` left it unassigned) wrongly parked them too, since
the old logic never distinguished "the real reviewer holds this" from
"someone else's task happens to be in status=review".
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.task import Task, TaskComment
from app.utils import ensure_aware, utcnow

# Nachpruefung 22.09.2026 (N2, Pruefbericht run7/07): a DB sample of
# 30 days of reviewer comments showed that bridges write AUTOMATIC comments
# under the reviewer's own name — e.g. poll.sh's turn-state detection posts
# 124 `blocker` comments in 30 days, the omp-bridge posts `progress` model-
# error classifications. Neither carries a real verdict, so neither may
# release the card. Only these types carry actual review content.
REVIEW_DONE_COMMENT_TYPES = frozenset({"feedback", "message", "reflection"})


async def review_still_parks(
    session: AsyncSession,
    task: Task,
    agent: Agent,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Whether `task` (status == "review") should still park `agent`.

    Rules, checked in order: the kill switch off means always True (legacy,
    unconditional). A live turn (agent.status == "working" with a heartbeat
    younger than TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS, same signal as
    dispatch Guard 3) always parks too — even past the grace window or
    after a comment — releasing mid-turn risks a bridge pasting the next
    prompt into a still-running session (Nachpruefung N3, poll.sh
    fail-open after READY_TIMEOUT_SEC=5s). Only the real reviewer is
    parkable (dispatch_intent == "review_handoff" AND assigned to this
    agent) — otherwise False immediately (e.g. the developer holding their
    own just-submitted card). Once the reviewer has posted a real-content
    comment (REVIEW_DONE_COMMENT_TYPES) since delivery, the review itself is
    done — only the approve/reject decision is outstanding — so it releases
    (False). Otherwise it parks (True) until settings.review_park_grace_minutes
    have passed since delivery. Anchor (Nachpruefung K1): ack_at — the
    moment the reviewer actually took delivery — falling back to
    dispatched_at, falling back to updated_at.
    """
    if not settings.review_park_grace_enabled:
        return True

    if agent.status == "working" and agent.last_seen_at is not None:
        from app.services.dispatch import TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS

        seen_age = (utcnow() - ensure_aware(agent.last_seen_at)).total_seconds()
        if seen_age < TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS:
            return True

    if task.dispatch_intent != "review_handoff" or task.assigned_agent_id != agent.id:
        return False

    anchor = task.ack_at or task.dispatched_at or task.updated_at
    if anchor is None:
        return True
    anchor = ensure_aware(anchor)

    commented = (
        await session.exec(
            select(TaskComment).where(
                TaskComment.task_id == task.id,
                TaskComment.author_agent_id == agent.id,
                TaskComment.comment_type.in_(REVIEW_DONE_COMMENT_TYPES),  # type: ignore[union-attr]
                TaskComment.created_at >= anchor,
            ).limit(1)
        )
    ).first()
    if commented is not None:
        return False

    now = ensure_aware(now) if now is not None else utcnow()
    age_minutes = (now - anchor).total_seconds() / 60
    return age_minutes < settings.review_park_grace_minutes
