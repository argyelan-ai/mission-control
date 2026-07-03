"""Tests for timestamp reset on agent reassignment.

When assigned_agent_id changes, dispatched_at and ack_at must be
reset — the new agent needs a fresh dispatch cycle. Otherwise the
task runner detects false ACK timeouts and the watchdog operates
on stale timestamps.
"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


# ── Path 1: Manual reassignment via UI ──────────────────────────────


@pytest.mark.asyncio
async def test_manual_reassignment_resets_dispatch_timestamps(
    auth_client, make_board, make_agent, make_task
):
    """PATCH assigned_agent_id → dispatched_at and ack_at must become None.

    Phase 29 / Wave 4 cleanup: before Phase 29, re-dispatch ran inline via
    rpc.chat_send and set dispatched_at synchronously in the PATCH response.
    Since Phase 29 the router sends re-dispatch as a background task
    (auto_dispatch_task via create_tracked_task) — dispatched_at stays None
    in the PATCH response and is only set later in dispatch_delivery.
    Clearing the old Cody timestamps is still a synchronous effect that
    we verify here.
    """
    board = await make_board(name="Dev", slug="dev-reassign-1")
    cody = await make_agent(name="Cody", board_id=board.id)
    rex = await make_agent(name="Rex", board_id=board.id)

    # Task was in_progress with Cody, with all timestamps set
    old_dispatch = datetime.utcnow() - timedelta(minutes=15)
    old_ack = datetime.utcnow() - timedelta(minutes=14)
    old_start = datetime.utcnow() - timedelta(minutes=14)
    task = await make_task(
        board.id,
        title="Feature X",
        status="in_progress",
        assigned_agent_id=cody.id,
        dispatched_at=old_dispatch,
        ack_at=old_ack,
        started_at=old_start,
    )

    with (
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
        patch("app.services.dispatch._build_dispatch_message", new_callable=AsyncMock, return_value="msg"),
    ):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"assigned_agent_id": str(rex.id)},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["assigned_agent_id"] == str(rex.id)
    # Both tracking timestamps reset — re-dispatch runs in the
    # background task, only fills dispatched_at again after a
    # successful dispatch_delivery.
    assert data["dispatched_at"] is None
    assert data["ack_at"] is None


# test_manual_reassignment_sets_dispatched_at_on_success removed (Phase 29 /
# Wave 4 cleanup): tested the synchronous rpc.chat_send success path in
# `tasks.py`. Since Phase 29, re-dispatch runs via auto_dispatch_task
# (background task) — the PATCH response never sees dispatched_at set
# inline; that only happens in dispatch_delivery.


@pytest.mark.asyncio
async def test_manual_reassignment_dispatch_fails_leaves_dispatched_at_none(
    auth_client, make_board, make_agent, make_task
):
    """Reassignment PATCH returns dispatched_at=None.

    Phase 29 / Wave 4: re-dispatch runs via create_tracked_task →
    auto_dispatch_task in the background. The PATCH response itself
    always has dispatched_at None. (If the background delivery fails,
    dispatched_at stays None permanently and the watchdog re-dispatches.)
    """
    board = await make_board(name="Dev", slug="dev-reassign-3")
    cody = await make_agent(name="Cody", board_id=board.id)
    rex = await make_agent(name="Rex", board_id=board.id)

    task = await make_task(
        board.id,
        title="Feature Z",
        status="in_progress",
        assigned_agent_id=cody.id,
        dispatched_at=datetime.utcnow() - timedelta(minutes=10),
        ack_at=datetime.utcnow() - timedelta(minutes=9),
    )

    with (
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
        patch("app.services.dispatch._build_dispatch_message", new_callable=AsyncMock, return_value="msg"),
    ):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"assigned_agent_id": str(rex.id)},
        )

    data = resp.json()
    assert data["dispatched_at"] is None
    assert data["ack_at"] is None


# ── Path 2+4: Review handoff (reviewer receives task) ──────────────────


@pytest.mark.asyncio
async def test_review_handoff_resets_dispatch_timestamps(
    auth_client, make_board, make_agent, make_task
):
    """review handoff to reviewer → dispatched_at/ack_at reset."""
    board = await make_board(name="Dev", slug="dev-review-handoff")
    dev = await make_agent(name="Cody", board_id=board.id)
    reviewer = await make_agent(name="Rex", board_id=board.id)

    task = await make_task(
        board.id,
        title="Review Me",
        status="in_progress",
        assigned_agent_id=dev.id,
        dispatched_at=datetime.utcnow() - timedelta(minutes=20),
        ack_at=datetime.utcnow() - timedelta(minutes=19),
        started_at=datetime.utcnow() - timedelta(minutes=19),
    )

    with (
        patch("app.routers.agent_scoped._find_reviewer", new_callable=AsyncMock, return_value=reviewer),
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
    ):
        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "review"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["assigned_agent_id"] == str(reviewer.id)
    assert data["ack_at"] is None  # Reviewer hasn't ACK'd yet


# ── Path 3+5: Review rejection (dev gets it back, dev free) ─────────


@pytest.mark.asyncio
async def test_review_rejection_dev_free_resets_dispatch_timestamps(
    auth_client, make_board, make_agent, make_task
):
    """Review rejected, dev is free → timestamps reset for new dispatch cycle."""
    board = await make_board(name="Dev", slug="dev-rejection-ts")
    dev = await make_agent(name="Cody", board_id=board.id)

    task = await make_task(
        board.id,
        title="Rejected Review",
        status="review",
        assigned_agent_id=dev.id,
        dispatched_at=datetime.utcnow() - timedelta(minutes=30),
        ack_at=datetime.utcnow() - timedelta(minutes=29),
    )

    with (
        patch("app.routers.agent_scoped._find_last_developer", new_callable=AsyncMock, return_value=dev),
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
        patch("app.services.dispatch._build_dispatch_message", new_callable=AsyncMock, return_value="msg"),
        patch("app.services.task_queue.get_redis") as mock_get_redis,
    ):
        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=0)
        mock_redis.expire = AsyncMock()
        mock_get_redis.return_value = mock_redis

        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ack_at"] is None  # New dispatch cycle
