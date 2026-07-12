"""Test: POST /tasks accepts skip_review and persists it to the Task model.

Mirrors the TaskCreate schema extension in routers/tasks.py.
Task.skip_review is enforced in services/work_context.py (line 477):
if the flag is set, the review gate is bypassed on the done transition.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_board() -> uuid.UUID:
    from app.models.board import Board

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="SR Test Board", slug=f"sr-{board_id.hex[:8]}")
        s.add(board)
        await s.commit()
    return board_id


@pytest.mark.asyncio
async def test_post_task_with_skip_review_persists(auth_client: AsyncClient):
    """POST /boards/{id}/tasks with skip_review=true → task.skip_review is True."""
    board_id = await _create_board()

    with (
        patch("app.routers.tasks.auto_dispatch_task", new_callable=AsyncMock),
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
    ):
        resp = await auth_client.post(
            f"/api/v1/boards/{board_id}/tasks",
            json={
                "title": "Scheduled Digest",
                "skip_review": True,
            },
        )

    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    # Verify the flag was actually stored in the DB
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, uuid.UUID(task_id))
        assert task is not None
        assert task.skip_review is True


@pytest.mark.asyncio
async def test_post_task_skip_review_defaults_false(auth_client: AsyncClient):
    """POST without skip_review → task.skip_review defaults to False."""
    board_id = await _create_board()

    with (
        patch("app.routers.tasks.auto_dispatch_task", new_callable=AsyncMock),
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
    ):
        resp = await auth_client.post(
            f"/api/v1/boards/{board_id}/tasks",
            json={"title": "Regular Task"},
        )

    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, uuid.UUID(task_id))
        assert task is not None
        assert task.skip_review is False
