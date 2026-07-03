"""Regressions from the demo-seed test run (2026-07-02, migration 0132).

1. approvals.agent_id must be nullable — the watchdog creates
   review_stuck approvals for tasks WITHOUT an assigned agent. With NOT NULL
   every watchdog tick crashed (commit error -> Redis dedup never set
   -> infinite retry).

2. delete_board() is a soft delete, boards.slug is UNIQUE — without
   renaming the slug, a deleted board blocks its slug forever
   (re-creation -> 500 UniqueViolation).
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_approval_without_agent_persists(make_board, make_task):
    """review_stuck approval for an unassigned task must not crash."""
    from app.models.approval import Approval

    board = await make_board(slug="approval-null-agent")
    task = await make_task(board_id=board.id, status="review")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            board_id=board.id,
            task_id=task.id,
            agent_id=None,  # Task has no agent — exactly the crash case
            action_type="review_stuck",
            description="Review haengt seit 185 Min.",
        ))
        await s.commit()

        saved = (await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).first()
        assert saved is not None
        assert saved.agent_id is None


@pytest.mark.asyncio
async def test_deleted_board_frees_its_slug(auth_client: AsyncClient):
    """Archiving renames the slug — re-creation with the same slug works."""
    payload = {"name": "Demo", "slug": "demo-slug-reuse"}

    first = await auth_client.post("/api/v1/boards", json=payload)
    assert first.status_code in (200, 201), first.text
    board_id = first.json()["id"]

    deleted = await auth_client.delete(f"/api/v1/boards/{board_id}")
    assert deleted.status_code == 204, deleted.text

    second = await auth_client.post("/api/v1/boards", json=payload)
    assert second.status_code in (200, 201), second.text
    assert second.json()["id"] != board_id

    # The archived board carries the renamed slug
    from app.models.board import Board
    async with AsyncSession(test_engine) as s:
        old = await s.get(Board, uuid.UUID(board_id))
        assert old.is_archived is True
        assert old.slug.startswith("demo-slug-reuse--archived-")
