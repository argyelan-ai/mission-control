"""Tests for workspace port allocation.

Tests:
1. _allocate_port() returns 4200 when no ports are in use
2. Used ports get skipped
3. Tasks with status=done get ignored (port is free)
4. Port gets released on status → done (via PATCH API)
5. Port gets released on status → failed (via PATCH API)
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.dispatch import _allocate_port


# ── Direct Function Tests ────────────────────────────────────────────────

class TestAllocatePort:

    @pytest.mark.asyncio
    async def test_allocate_port_first_free(self, session: AsyncSession):
        """No ports in use → first port is 4200."""
        port = await _allocate_port(session)
        assert port == 4200

    @pytest.mark.asyncio
    async def test_allocate_port_skips_used(self, session: AsyncSession, make_board, make_task):
        """Used ports get skipped → next free port."""
        board = await make_board("Port Board", slug="port-board")

        # Two tasks with occupied ports (active statuses)
        await make_task(
            board.id, title="Task 4200",
            status="in_progress", workspace_port=4200,
        )
        await make_task(
            board.id, title="Task 4201",
            status="review", workspace_port=4201,
        )

        port = await _allocate_port(session)
        assert port == 4202

    @pytest.mark.asyncio
    async def test_allocate_port_ignores_done_tasks(
        self, session: AsyncSession, make_board, make_task,
    ):
        """Tasks with status=done get ignored → port is free again."""
        board = await make_board("Done Board", slug="done-board")

        # Task with port 4200 but status=done → port is free
        await make_task(
            board.id, title="Done Task",
            status="done", workspace_port=4200,
        )

        port = await _allocate_port(session)
        assert port == 4200

    @pytest.mark.asyncio
    async def test_allocate_port_ignores_failed_tasks(
        self, session: AsyncSession, make_board, make_task,
    ):
        """Tasks with status=failed get ignored → port is free."""
        board = await make_board("Failed Board", slug="failed-board")

        await make_task(
            board.id, title="Failed Task",
            status="failed", workspace_port=4200,
        )

        port = await _allocate_port(session)
        assert port == 4200

    @pytest.mark.asyncio
    async def test_allocate_port_mixed_statuses(
        self, session: AsyncSession, make_board, make_task,
    ):
        """Mix of active and completed tasks — only active ones count."""
        board = await make_board("Mixed Board", slug="mixed-board")

        # Active → port occupied
        await make_task(
            board.id, title="Active 4200",
            status="in_progress", workspace_port=4200,
        )
        # Done → port free
        await make_task(
            board.id, title="Done 4201",
            status="done", workspace_port=4201,
        )
        # Active → port occupied
        await make_task(
            board.id, title="Active 4202",
            status="inbox", workspace_port=4202,
        )

        port = await _allocate_port(session)
        # 4200 occupied, 4201 free (done), so 4201
        assert port == 4201


# ── API Integration (Port Release via PATCH) ─────────────────────────────

class TestPortReleaseOnStatusChange:

    @pytest.mark.asyncio
    async def test_port_released_on_done(
        self, auth_client, make_board, make_task,
    ):
        """Set task to done → workspace_port becomes None."""
        board = await make_board("Release Board", slug="release-board")
        task = await make_task(
            board.id, title="Port Release Done",
            status="in_progress", workspace_port=4210,
        )

        with (
            patch("app.services.activity.broadcast", new_callable=AsyncMock),
            patch("app.services.task_lifecycle.trigger_auto_memory"),
            patch("app.services.task_lifecycle.trigger_feedback_lesson", new_callable=AsyncMock),
        ):
            resp = await auth_client.patch(
                f"/api/v1/boards/{board.id}/tasks/{task.id}",
                json={"status": "done"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["workspace_port"] is None

    @pytest.mark.asyncio
    async def test_port_released_on_failed(
        self, auth_client, make_board, make_task,
    ):
        """Set task to failed → workspace_port becomes None."""
        board = await make_board("Release Board 2", slug="release-board-2")
        task = await make_task(
            board.id, title="Port Release Failed",
            status="in_progress", workspace_port=4211,
        )

        with (
            patch("app.services.activity.broadcast", new_callable=AsyncMock),
            patch("app.services.task_lifecycle.trigger_auto_memory"),
            patch("app.services.task_lifecycle.trigger_feedback_lesson", new_callable=AsyncMock),
        ):
            resp = await auth_client.patch(
                f"/api/v1/boards/{board.id}/tasks/{task.id}",
                json={"status": "failed"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["workspace_port"] is None

    @pytest.mark.asyncio
    async def test_port_kept_on_review(
        self, auth_client, make_board, make_task,
    ):
        """Set task to review → workspace_port stays intact."""
        board = await make_board("Keep Board", slug="keep-board")
        task = await make_task(
            board.id, title="Port Keep Review",
            status="in_progress", workspace_port=4212,
        )

        with (
            patch("app.services.activity.broadcast", new_callable=AsyncMock),
            patch("app.services.task_lifecycle.trigger_auto_memory"),
            patch("app.services.task_lifecycle.trigger_feedback_lesson", new_callable=AsyncMock),
        ):
            resp = await auth_client.patch(
                f"/api/v1/boards/{board.id}/tasks/{task.id}",
                json={"status": "review"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["workspace_port"] == 4212
