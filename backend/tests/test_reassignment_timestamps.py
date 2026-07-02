"""Tests fuer Timestamp-Reset bei Agent-Reassignment.

Wenn assigned_agent_id wechselt, muessen dispatched_at und ack_at
zurueckgesetzt werden — der neue Agent braucht einen frischen
Dispatch-Zyklus. Sonst erkennt der Task Runner falsche ACK-Timeouts
und der Watchdog arbeitet mit veralteten Timestamps.
"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


# ── Path 1: Manuelles Reassignment via UI ──────────────────────────────


@pytest.mark.asyncio
async def test_manual_reassignment_resets_dispatch_timestamps(
    auth_client, make_board, make_agent, make_task
):
    """PATCH assigned_agent_id → dispatched_at und ack_at muessen None werden.

    Phase 29 / Wave 4 cleanup: vor Phase 29 lief der Re-Dispatch inline
    via rpc.chat_send und setzte dispatched_at synchron im PATCH-Response.
    Seit Phase 29 schickt der Router den Re-Dispatch als Background-Task
    (auto_dispatch_task via create_tracked_task) — dispatched_at bleibt im
    PATCH-Response None und wird erst spaeter in dispatch_delivery gesetzt.
    Das Loeschen alter Cody-Timestamps ist trotzdem ein synchroner Effekt
    den wir hier verifizieren.
    """
    board = await make_board(name="Dev", slug="dev-reassign-1")
    cody = await make_agent(name="Cody", board_id=board.id)
    rex = await make_agent(name="Rex", board_id=board.id)

    # Task war bei Cody in_progress mit allen Timestamps gesetzt
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
    # Beide Tracking-Timestamps zurueckgesetzt — Re-Dispatch laeuft im
    # Background-Task, fuellt dispatched_at erst nach erfolgreichem
    # dispatch_delivery wieder.
    assert data["dispatched_at"] is None
    assert data["ack_at"] is None


# test_manual_reassignment_sets_dispatched_at_on_success entfernt (Phase 29 /
# Wave 4 cleanup): testete den synchronen rpc.chat_send-Erfolgs-Pfad in
# `tasks.py`. Seit Phase 29 laeuft der Re-Dispatch via auto_dispatch_task
# (Background-Task) — der PATCH-Response sieht dispatched_at nie inline
# gesetzt, das geschieht erst in dispatch_delivery.


@pytest.mark.asyncio
async def test_manual_reassignment_dispatch_fails_leaves_dispatched_at_none(
    auth_client, make_board, make_agent, make_task
):
    """Re-Assignment-PATCH gibt dispatched_at=None zurueck.

    Phase 29 / Wave 4: Re-Dispatch laeuft via create_tracked_task →
    auto_dispatch_task im Background. Der PATCH-Response selbst hat
    dispatched_at immer None. (Falls die Background-Lieferung scheitert,
    bleibt dispatched_at dauerhaft None und der Watchdog re-dispatcht.)
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


# ── Path 2+4: Review-Handoff (Reviewer bekommt Task) ──────────────────


@pytest.mark.asyncio
async def test_review_handoff_resets_dispatch_timestamps(
    auth_client, make_board, make_agent, make_task
):
    """review-Handoff an Reviewer → dispatched_at/ack_at zurueckgesetzt."""
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
    assert data["ack_at"] is None  # Reviewer hat noch nicht ACK'd


# ── Path 3+5: Review-Rejection (Dev bekommt zurueck, dev frei) ─────────


@pytest.mark.asyncio
async def test_review_rejection_dev_free_resets_dispatch_timestamps(
    auth_client, make_board, make_agent, make_task
):
    """Review abgelehnt, Dev ist frei → Timestamps fuer neuen Dispatch-Zyklus zurueckgesetzt."""
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
    assert data["ack_at"] is None  # Neuer Dispatch-Zyklus
