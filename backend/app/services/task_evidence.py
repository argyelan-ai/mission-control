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
