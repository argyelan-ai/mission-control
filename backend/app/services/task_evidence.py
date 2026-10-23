"""Server-observed work evidence for a task.

The watchdogs must never derive liveness from fields the observed party
writes about itself: ``agent.last_task_activity_at`` is stamped by every
heartbeat carrying ``status="working"``, and that status is derived from
mere lock-file existence (``docker/omp-bridge/bridge.py`` task_active
lambda) — a dead turn with a surviving lock file looks alive forever
(2026-09-18 incident, card b2ea802f: 9h blind).

The one liveness signal the agent CANNOT fabricate by existing is the
harvested transcript: ``ModelUsageEvent`` rows are written by the backend
token harvester (services/token_harvester.py), attributed to the dispatch
task_id, and one row per assistant message. No LLM turn ran → no row
arrives → the newest timestamp goes stale. This is the same evidence the
poll-orphan redispatch already trusts (routers/agents.py
_maybe_redispatch_orphaned_run).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import func as sqlfunc
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.utils import ensure_aware

logger = logging.getLogger("mc.task_evidence")


async def latest_model_event_at(session: AsyncSession, task_id) -> datetime | None:
    """Newest harvested-transcript timestamp for this task, or None.

    None means "no harvest evidence exists for this task" — callers treat
    that as fall back to legacy signals, never as "dead": a runtime whose
    transcripts are not harvested would otherwise look permanently dead.
    """
    from app.models.model_usage import ModelUsageEvent

    ts = (
        await session.exec(
            select(sqlfunc.max(ModelUsageEvent.ts)).where(
                ModelUsageEvent.task_id == task_id
            )
        )
    ).one()
    return ensure_aware(ts) if ts is not None else None


async def last_real_activity_at(session: AsyncSession, task, children=()) -> datetime | None:
    """Latest real activity on a card — server-observed evidence only.

    Single source for the watchdog's silent-card clock
    (``TaskMonitorMixin._silent_card_last_activity_at`` delegates here) and
    for any read-only "last sign of life" display (e.g. the Bench Studio
    entry card), so both always agree on what counts as activity.

    Counts:
    - agent/user comments on this task (system/watchdog_notify ignored —
      counting those as activity is what restacks reminders; a chat
      session's reply, ``chat_reply``, ignored too — it proves the chat
      answered, not that the run lives: "how far are you?" on a hanging
      run must not push the alarm back, round-3 code review r1 S3)
    - the newest harvested ModelUsageEvent.ts for this task — the one
      liveness signal the agent cannot fabricate by existing (a
      working-heartbeat is derived from mere lock-file existence, and
      that file survives a dead turn — 2026-09-18, card b2ea802f: 9h
      blind because the heartbeat kept stamping
      agent.last_task_activity_at)
    - started_at (work began; first-set-wins, immune to recovery
      re-deliveries — unlike ack_at, which the poll handler re-sets on
      EVERY prompt re-delivery and tier-3 recovery resets + redelivery
      re-sets, zeroing this clock without any work happening)
    - last child completed_at/updated_at (so a parent whose children
      just finished is not flagged for 30 min)
    Deliberately omitted:
    - updated_at (any metadata PATCH resets it)
    - last_seen_at (wrapper heartbeat ≠ a turn)
    - agent.last_task_activity_at (self-reported: stamped by every
      status="working" heartbeat, whose status comes from file
      existence — the observed party vouching for itself)
    - task.ack_at (re-delivery latch: re-set by the poll recovery
      branch, cleared+re-set by tier-3 — a repair attempt must not
      reset a silence clock)
    """
    from app.comment_types import CHAT_REPLY_COMMENT_TYPE
    from app.models.task import TaskComment

    last_real_comment = (await session.exec(
        select(TaskComment)
        .where(
            TaskComment.task_id == task.id,
            TaskComment.author_type != "system",
            TaskComment.comment_type != CHAT_REPLY_COMMENT_TYPE,
        )
        .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )).first()

    model_event_at = await latest_model_event_at(session, task.id)

    child_times = [(c.completed_at or c.updated_at) for c in children]

    candidates = [
        ensure_aware(v)
        for v in (
            last_real_comment.created_at if last_real_comment else None,
            model_event_at,
            task.started_at,
            *child_times,
        )
        if v is not None
    ]
    return max(candidates) if candidates else None
