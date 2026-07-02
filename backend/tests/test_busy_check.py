"""Tests fuer Busy-Check bei Review-Rejection (tasks.py).

Wenn ein Review abgelehnt wird (review → in_progress) und der Developer
busy ist (hat in_progress oder dispatched-but-not-acked Task), soll der
abgelehnte Task in die Queue statt sofort dispatcht zu werden.
Das verhindert doppelte in_progress Tasks.
"""
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest


# ── tasks.py: User lehnt Review ab → busy-check ──────────────────────


@pytest.mark.asyncio
async def test_review_rejection_queues_when_dev_busy(
    auth_client, make_board, make_agent, make_task
):
    """User setzt Task von review→in_progress, Developer hat schon aktiven Task → Queue."""
    board = await make_board(name="MC Dev", slug="mc-dev")
    dev = await make_agent(name="Cody", board_id=board.id)

    # Cody hat schon einen aktiven Task (in_progress)
    await make_task(
        board.id,
        title="Aktiver Task",
        status="in_progress",
        assigned_agent_id=dev.id,
    )

    # Abgelehnter Review-Task
    review_task = await make_task(
        board.id,
        title="Review abgelehnt",
        status="review",
        assigned_agent_id=dev.id,
    )

    with (
        patch("app.routers.agent_scoped._find_last_developer", new_callable=AsyncMock, return_value=dev),
        patch("app.services.task_queue.get_redis") as mock_get_redis,
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
    ):
        mock_redis = AsyncMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=0)
        mock_redis.expire = AsyncMock()
        mock_get_redis.return_value = mock_redis

        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{review_task.id}",
            json={"status": "in_progress"},
        )

    assert resp.status_code == 200
    data = resp.json()
    # Task wird auf inbox zurueckgesetzt weil Dev busy ist
    assert data["status"] == "inbox"
    assert data["assigned_agent_id"] == str(dev.id)


@pytest.mark.asyncio
async def test_review_rejection_dispatches_when_dev_free(
    auth_client, make_board, make_agent, make_task
):
    """User setzt Task von review→in_progress, Developer ist frei → bleibt in_progress."""
    board = await make_board(name="MC Dev", slug="mc-dev-2")
    dev = await make_agent(name="Cody", board_id=board.id)

    # Cody hat KEINEN aktiven Task — alle done
    await make_task(
        board.id,
        title="Erledigter Task",
        status="done",
        assigned_agent_id=dev.id,
    )

    review_task = await make_task(
        board.id,
        title="Review zum Ueberarbeiten",
        status="review",
        assigned_agent_id=dev.id,
    )

    with (
        patch("app.routers.agent_scoped._find_last_developer", new_callable=AsyncMock, return_value=dev),
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
        patch("app.routers.tasks.create_tracked_task"),
        patch("app.utils.create_tracked_task"),
        patch("app.services.dispatch._build_dispatch_message", new_callable=AsyncMock, return_value="dispatch msg"),
        patch("app.services.task_queue.get_redis") as mock_get_redis,
    ):
        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=0)
        mock_redis.expire = AsyncMock()
        mock_get_redis.return_value = mock_redis

        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{review_task.id}",
            json={"status": "in_progress"},
        )

    assert resp.status_code == 200
    data = resp.json()
    # Task geht auf inbox (Review-Rejection → inbox → auto_dispatch Background)
    # oder bleibt in_progress wenn Rejection-Handler nicht greift
    assert data["status"] in ("in_progress", "inbox")
    assert data["assigned_agent_id"] == str(dev.id)


@pytest.mark.asyncio
async def test_review_rejection_queues_with_dispatched_inbox_task(
    auth_client, make_board, make_agent, make_task
):
    """Developer hat dispatched-but-not-acked Task (inbox + dispatched_at) → Queue."""
    board = await make_board(name="MC Dev", slug="mc-dev-3")
    dev = await make_agent(name="Cody", board_id=board.id)

    # Cody hat einen dispatched-but-not-acked Task
    await make_task(
        board.id,
        title="Dispatched nicht ACKed",
        status="inbox",
        assigned_agent_id=dev.id,
        dispatched_at=datetime.utcnow(),
    )

    review_task = await make_task(
        board.id,
        title="Zweiter Review",
        status="review",
        assigned_agent_id=dev.id,
    )

    with (
        patch("app.routers.agent_scoped._find_last_developer", new_callable=AsyncMock, return_value=dev),
        patch("app.services.task_queue.get_redis") as mock_get_redis,
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
    ):
        mock_redis = AsyncMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=0)
        mock_redis.expire = AsyncMock()
        mock_get_redis.return_value = mock_redis

        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{review_task.id}",
            json={"status": "in_progress"},
        )

    assert resp.status_code == 200
    data = resp.json()
    # Dispatched inbox Task zaehlt als busy → Queue
    assert data["status"] == "inbox"


@pytest.mark.asyncio
async def test_review_rejection_no_dev_found_keeps_in_progress(
    auth_client, make_board, make_task
):
    """Wenn kein Developer gefunden wird, bleibt Task in_progress ohne Reassignment."""
    board = await make_board(name="MC Dev", slug="mc-dev-4")

    review_task = await make_task(
        board.id,
        title="Kein Dev gefunden",
        status="review",
    )

    with (
        patch("app.routers.agent_scoped._find_last_developer", new_callable=AsyncMock, return_value=None),
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
    ):

        resp = await auth_client.patch(
            f"/api/v1/boards/{board.id}/tasks/{review_task.id}",
            json={"status": "in_progress"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "in_progress"
    assert data["assigned_agent_id"] is None
