"""Approving a blocker on a stopped/held card must not strand it in inbox.

Live finding 2026-11-02: a bench time limit stopped a card (stop_task_run,
changed_by="system" → blocked + run_control="stopped"), an empty blocker
card was raised for it, and approving that card moved the task
blocked → inbox with run_control still "stopped".
redispatch_after_blocker_answer then skipped (task_still_reactivatable), so
the card sat in inbox with no run. The Telegram button path already refuses
here ("task_held"); the PATCH and quick-resolve paths now refuse too, before
touching anything — the operator releases the card via Resume.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine
from tests.test_blocker_approval import _create_blocker_data
from tests.test_blocker_redispatch_wiring import (
    _capture_tracked_task,
    _make_pending_blocker_approval,
)


async def _stop_task(task_id, run_control):
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        task.run_control = run_control
        s.add(task)
        await s.commit()


async def _task_and_approval(task_id, approval_id):
    from app.models.approval import Approval
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Task, task_id), await s.get(Approval, approval_id)


@pytest.mark.parametrize("run_control", ["stopped", "manual_hold"])
@pytest.mark.asyncio
async def test_patch_approve_on_stopped_card_refuses_and_keeps_it_blocked(
    auth_client, fake_redis, run_control,
):
    data = await _create_blocker_data(task_status="blocked")
    await _stop_task(data["task"].id, run_control)
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

    assert resp.status_code == 409
    assert "Resume" in resp.json()["detail"]
    assert captured == [], "no redispatch may be scheduled for a stopped card"
    task, approval = await _task_and_approval(data["task"].id, approval_id)
    assert task.status == "blocked", "card must not be stranded in inbox"
    assert task.run_control == run_control
    assert approval.status == "pending"


@pytest.mark.asyncio
async def test_patch_reject_on_stopped_card_still_fails_it(auth_client, fake_redis):
    """Only approve is refused — rejecting (cancel the card) stays possible."""
    data = await _create_blocker_data(task_status="blocked")
    await _stop_task(data["task"].id, "stopped")
    approval_id = await _make_pending_blocker_approval(data)

    with patch("app.routers.approvals.emit_event", new_callable=AsyncMock):
        resp = await auth_client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"status": "rejected"},
        )

    assert resp.status_code == 200
    task, approval = await _task_and_approval(data["task"].id, approval_id)
    assert task.status == "failed"
    assert approval.status == "rejected"


@pytest.mark.asyncio
async def test_quick_resolve_approve_on_stopped_card_refuses_and_keeps_it_blocked(
    client, fake_redis,
):
    data = await _create_blocker_data(task_status="blocked")
    await _stop_task(data["task"].id, "stopped")
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

    assert resp.status_code == 409
    assert "Resume" in resp.text
    assert captured == []
    task, approval = await _task_and_approval(data["task"].id, approval_id)
    assert task.status == "blocked"
    assert task.run_control == "stopped"
    assert approval.status == "pending"
