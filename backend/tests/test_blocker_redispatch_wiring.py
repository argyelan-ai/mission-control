"""Wiring tests for the two approvals.py call sites that schedule
redispatch_after_blocker_answer (PR #556 follow-up — Rex's blocker).

The predicate (task_lifecycle.task_still_reactivatable) and the wrapper
(dispatch.redispatch_after_blocker_answer) are both unit-tested elsewhere
(test_blocker_redispatch_race.py) — but nothing in the repo drove the real
approval-resolution endpoints far enough to notice if either call site
quietly regressed to a bare `create_tracked_task(auto_dispatch_task(...))`
(the pre-PR shape that caused incident 2026-09-13 / card 4c9bb492). These
two tests run the real resolve path, capture the scheduled background
coroutine instead of letting it fire immediately, move the card into
"review" in the gap (mirrors the incident's timing), then run the captured
coroutine and assert on the skip *event* — not just on task.status, which
in a real disarmed-guard run stays on "review" too (the fallback to
"inbox" happens further down the delivery path, not here) and would stay
green even with the guard fully removed.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine
from tests.test_blocker_approval import _create_blocker_data


def _capture_tracked_task(captured):
    """Stand-in for app.utils.create_tracked_task: keep the coroutine
    instead of scheduling it on the running loop, so the test controls
    exactly when it runs."""

    def _capture(coro, name=None):
        captured.append(coro)

        class _DummyTask:
            def add_done_callback(self, *a, **k):
                pass

        return _DummyTask()

    return _capture


async def _run_captured_redispatch(coro):
    """Run the captured redispatch coroutine against a fresh
    auto_dispatch_task/emit_event mock — this is the "later" half of the
    race, after the card moved on in the gap."""
    with (
        patch("app.services.dispatch.engine", test_engine),
        patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch,
        patch("app.services.dispatch.emit_event", new_callable=AsyncMock) as mock_emit,
    ):
        await coro
    return mock_dispatch, mock_emit


async def _make_pending_blocker_approval(data):
    from app.models.approval import Approval
    from app.utils import utcnow

    approval_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            id=approval_id,
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            action_type="blocker_decision",
            description="Test blocker",
            status="pending",
            expires_at=utcnow() + timedelta(hours=24),
        )
        s.add(approval)
        await s.commit()
    return approval_id


async def _move_task_to_review_in_the_gap(task_id):
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.status == "inbox", "resolve path must land the task in inbox before the gap"
        task.status = "review"
        s.add(task)
        await s.commit()


@pytest.mark.asyncio
async def test_patch_endpoint_wiring_skips_stale_redispatch(auth_client, fake_redis):
    """approvals.py:409 (PATCH /approvals/{id}, approved) must schedule
    redispatch_after_blocker_answer, not a bare auto_dispatch_task.

    Sabotage: revert this call site's create_tracked_task(...) argument
    back to `auto_dispatch_task(task.id, task.board_id)` — this test goes
    red (mock_dispatch.called becomes True, no skip event) while the rest
    of the suite stays green (Rex's S5)."""
    data = await _create_blocker_data(task_status="blocked")
    approval_id = await _make_pending_blocker_approval(data)

    captured = []
    with (
        patch("app.routers.approvals.emit_event", new_callable=AsyncMock),
        patch("app.utils.create_tracked_task", _capture_tracked_task(captured)),
    ):
        resp = await auth_client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"status": "approved", "resolver_note": "weiter"},
        )
    assert resp.status_code == 200
    assert len(captured) == 1, "resolve_approval must schedule exactly one background coroutine"

    await _move_task_to_review_in_the_gap(data["task"].id)

    mock_dispatch, mock_emit = await _run_captured_redispatch(captured[0])

    assert not mock_dispatch.called, "guard must skip — task moved on to review in the gap"
    events = [c.args[1] for c in mock_emit.call_args_list if len(c.args) > 1]
    assert "task.blocker_redispatch_skipped" in events


@pytest.mark.asyncio
async def test_telegram_quick_resolve_wiring_skips_stale_redispatch(client, fake_redis):
    """approvals.py:965 (POST quick-resolve/confirm, approve) is the second
    caller of the same guarded wrapper — the actual incident channel
    (4c9bb492/G5) went through Telegram, not the PATCH endpoint.

    Sabotage: revert this call site the same way — red, no skip event
    (Rex's S6), independently of whether :409 is fixed or broken."""
    data = await _create_blocker_data(task_status="blocked")
    approval_id = await _make_pending_blocker_approval(data)

    captured = []
    with (
        patch("app.routers.approvals.consume_action_token") as mock_consume,
        patch("app.routers.approvals.emit_event", new_callable=AsyncMock),
        patch("app.utils.create_tracked_task", _capture_tracked_task(captured)),
        patch("app.routers.approvals.telegram_bot") as mock_tg,
    ):
        mock_consume.return_value = {"approval_id": str(approval_id), "action": "approve"}
        mock_tg.update_resolved_telegram = AsyncMock()
        resp = await client.post(
            f"/api/v1/approvals/{approval_id}/quick-resolve/confirm",
            data={"token": "irrelevant-mocked"},
        )
    assert resp.status_code == 200
    assert len(captured) == 1, "quick_resolve_confirm must schedule exactly one background coroutine"

    await _move_task_to_review_in_the_gap(data["task"].id)

    mock_dispatch, mock_emit = await _run_captured_redispatch(captured[0])

    assert not mock_dispatch.called, "guard must skip — task moved on to review in the gap"
    events = [c.args[1] for c in mock_emit.call_args_list if len(c.args) > 1]
    assert "task.blocker_redispatch_skipped" in events
