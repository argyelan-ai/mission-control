"""GET /activity?exclude_event_type=… leaves those types out ON THE SERVER.

The Home stream asks for its last N events without the plumbing types
(silent dispatch retries, stale updates). Filtering after the fetch could
show "no activity" on a busy board: N plumbing rows in a row, real events
further back (critic 21.11.2026)."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.utils import utcnow
from tests.conftest import test_engine


async def _seed(board_id: uuid.UUID) -> None:
    now = utcnow()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # The real event is the OLDEST; five plumbing rows sit on top of it.
        s.add(ActivityEvent(event_type="task.done", title="real", board_id=board_id,
                            created_at=now - timedelta(minutes=10)))
        for i in range(5):
            s.add(ActivityEvent(event_type="task.dispatch_attempt_rotated", title=f"retry {i}",
                                board_id=board_id, created_at=now - timedelta(minutes=i)))
        s.add(ActivityEvent(event_type="task.stale_update_rejected", title="stale",
                            board_id=board_id, created_at=now))
        await s.commit()


async def test_excluded_types_do_not_eat_the_limit(auth_client):
    board_id = uuid.uuid4()
    await _seed(board_id)
    resp = await auth_client.get("/api/v1/activity", params=[
        ("board_id", str(board_id)), ("limit", "3"),
        ("exclude_event_type", "task.dispatch_attempt_rotated"),
        ("exclude_event_type", "task.stale_update_rejected"),
    ])
    assert resp.status_code == 200, resp.text
    assert [e["title"] for e in resp.json()] == ["real"]


async def test_without_exclusion_everything_comes(auth_client):
    board_id = uuid.uuid4()
    await _seed(board_id)
    resp = await auth_client.get("/api/v1/activity", params={"board_id": str(board_id), "limit": 50})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 7
