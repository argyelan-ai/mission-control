"""Watchdog review-stuck escalation must re-check the task's status right
before creating the operator approval, not rely on the ORM object loaded at
the top of `_check_review_tasks`.

Root cause of a recurring "review_stuck" false-alarm: `_check_review_tasks`
selects every `status == "review"` task once at the start of the tick, then
loops through them with several `await` points (redis, session.commit()).
Every session in this codebase runs with `expire_on_commit=False`
(app/database.py), so that in-memory Task object never refreshes for the
rest of the tick. If a *different* session finishes the review while this
tick is still running (e.g. the reviewer approves, or an operator resolves
it by hand), the watchdog never notices and still creates a `review_stuck`
approval for a card that's already done — the exact incident from
2026-09-13 (`f781d84f`, `ca3defcb`, `5e7c8087`, all long since `done`).

Fix: re-read the task's status fresh from the DB right before creating the
approval; skip (with a log entry) if it's no longer "review".
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models.approval import Approval


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


async def _finish_task_in_another_session(task_id) -> None:
    """Simulate a concurrent, unrelated session resolving the review while
    the watchdog tick is still working through its already-fetched list."""
    from app.models.task import Task
    async with _session() as s:
        t = await s.get(Task, task_id)
        t.status = "done"
        s.add(t)
        await s.commit()


def _redis_that_races(fake_redis, on_get):
    """Wrap fake_redis so the dedup-key GET (the first await after the
    escalation level is computed, right before the branch that creates the
    approval) triggers the concurrent status change — same injection point
    a real second request would land on mid-tick."""

    class _RacingRedis:
        def __getattr__(self, name):
            return getattr(fake_redis, name)

        async def get(self, key):
            if "review_approval" in key:
                await on_get()
            return await fake_redis.get(key)

    return _RacingRedis()


async def _run_check(redis, session):
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.task_monitor.get_redis",
               AsyncMock(return_value=redis)), \
         patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
         patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock) as emit:
        svc = WatchdogService()
        await svc._check_review_tasks(session)
    return emit


async def _review_stuck_approvals(task_id):
    from sqlmodel import select
    async with _session() as s:
        res = await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "review_stuck",
            )
        )
        return res.all()


@pytest.mark.asyncio
async def test_card_finished_between_detection_and_escalation_skips_approval(
    fake_redis, make_board, make_task, caplog,
):
    """The reported bug: card is 'review' when fetched, 'done' by the time
    the watchdog would create the approval → no approval, logged reason."""
    board = await make_board(name="Race", slug=f"race-{uuid.uuid4().hex[:8]}")
    task = await make_task(
        board_id=board.id, title="Card finishes mid-tick", status="review",
    )
    await _age_task(task.id, 200)  # past the 180min approval stage

    racing_redis = _redis_that_races(
        fake_redis, lambda: _finish_task_in_another_session(task.id)
    )

    with caplog.at_level("INFO", logger="mc.watchdog"):
        async with _session() as s:
            await _run_check(racing_redis, s)

    assert not await _review_stuck_approvals(task.id), (
        "a card that finished between detection and escalation must NOT "
        "get a review_stuck approval"
    )
    assert any("skipped" in r.message for r in caplog.records), (
        "skip must be logged with a reason, not silent"
    )


@pytest.mark.asyncio
async def test_genuinely_stuck_review_still_escalates(
    fake_redis, make_board, make_task,
):
    """Counter-test (Gegenprobe): a card that is STILL in review when the
    approval would be created must escalate exactly as before — the recheck
    must not swallow real escalations."""
    board = await make_board(name="RealStuck", slug=f"real-stuck-{uuid.uuid4().hex[:8]}")
    task = await make_task(
        board_id=board.id, title="Really stuck review", status="review",
    )
    await _age_task(task.id, 200)

    async with _session() as s:
        await _run_check(fake_redis, s)

    approvals = await _review_stuck_approvals(task.id)
    assert len(approvals) == 1, "genuinely stuck review must still create an approval"
