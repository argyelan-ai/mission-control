"""Watchdog review-stuck escalation must skip tasks on ARCHIVED boards.

Root cause of a recurring Discord "REVIEW STUCK" spam: demo-seed creates a
"Landing page hero section" task in `review` with no reviewer agent. The board
is later archived (soft-delete), but `_check_review_tasks` selected every
`status == "review"` task regardless of board state — so the orphan escalated
to the approval stage (>180min) and fired a `severity="warning"` event (→
Discord) every 2h, forever. Each demo-seed run added another immortal escalator.

Fix: the query joins Board and filters `is_archived == False`. These tests pin
that an archived-board review task never escalates, while a live-board one still
does (no regression to the real safety net).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.approval import Approval

# SQLite (test engine) stores datetimes tz-naive, while app.utils.utcnow() is
# tz-aware — in production the tz-aware Postgres timestamptz column keeps both
# sides aware, so `now - task.updated_at` works. In-memory we mirror that by
# running the whole check on a consistent naive-UTC clock.
def _naive_utcnow() -> datetime:
    return datetime.utcnow()


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _age_task(task_id, minutes: int) -> None:
    """Backdate updated_at so the review-stuck age math trips the approval stage."""
    from app.models.task import Task
    async with _session() as s:
        t = await s.get(Task, task_id)
        t.updated_at = _naive_utcnow() - timedelta(minutes=minutes)
        s.add(t)
        await s.commit()


async def _run_check(fake_redis, session):
    """Invoke the leaf check with fan-out (redis + emit_event) patched."""
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.task_monitor.get_redis",
               AsyncMock(return_value=fake_redis)), \
         patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
         patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock) as emit:
        svc = WatchdogService()
        await svc._check_review_tasks(session)
    return emit


async def _review_stuck_approvals(task_id):
    async with _session() as s:
        res = await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "review_stuck",
            )
        )
        return res.all()


@pytest.mark.asyncio
async def test_archived_board_review_task_never_escalates(
    fake_redis, make_board, make_task,
):
    """The reported bug: review task on an archived board → no approval, no event."""
    board = await make_board(
        name="Demo", slug=f"demo-{uuid.uuid4().hex[:6]}--archived-{uuid.uuid4().hex[:8]}",
        is_archived=True,
    )
    task = await make_task(
        board_id=board.id, title="Landing page hero section", status="review",
    )
    await _age_task(task.id, 200)  # past the 180min approval stage

    async with _session() as s:
        emit = await _run_check(fake_redis, s)

    assert not await _review_stuck_approvals(task.id), (
        "archived-board review task must NOT create a review_stuck approval"
    )
    stuck_events = [c for c in emit.call_args_list
                    if len(c.args) > 1 and c.args[1] == "task.review_stuck"]
    assert not stuck_events, "archived-board task must NOT fire a Discord warning"


@pytest.mark.asyncio
async def test_live_board_review_task_still_escalates(
    fake_redis, make_board, make_task,
):
    """Regression guard: a LIVE-board stuck review still escalates (safety net intact)."""
    board = await make_board(name="Live", slug=f"live-{uuid.uuid4().hex[:6]}")
    task = await make_task(
        board_id=board.id, title="Real stuck review", status="review",
    )
    await _age_task(task.id, 200)

    async with _session() as s:
        await _run_check(fake_redis, s)

    approvals = await _review_stuck_approvals(task.id)
    assert len(approvals) == 1, "live-board stuck review must still create an approval"
