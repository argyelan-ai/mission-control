"""Tests fuer Workspace Port Allocation.

Testet:
1. _allocate_port() gibt 4200 zurueck wenn keine Ports belegt
2. Belegte Ports werden uebersprungen
3. Tasks mit status=done werden ignoriert (Port ist frei)
4. Port wird freigegeben bei status → done (via PATCH API)
5. Port wird freigegeben bei status → failed (via PATCH API)
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
        """Keine Ports belegt → erster Port ist 4200."""
        port = await _allocate_port(session)
        assert port == 4200

    @pytest.mark.asyncio
    async def test_allocate_port_skips_used(self, session: AsyncSession, make_board, make_task):
        """Belegte Ports werden uebersprungen → naechster freier Port."""
        board = await make_board("Port Board", slug="port-board")

        # Zwei Tasks mit belegten Ports (aktive Status)
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
        """Tasks mit status=done werden ignoriert → Port ist wieder frei."""
        board = await make_board("Done Board", slug="done-board")

        # Task mit Port 4200 aber status=done → Port ist frei
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
        """Tasks mit status=failed werden ignoriert → Port ist frei."""
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
        """Mix aus aktiven und abgeschlossenen Tasks — nur aktive zaehlen."""
        board = await make_board("Mixed Board", slug="mixed-board")

        # Aktiv → Port belegt
        await make_task(
            board.id, title="Active 4200",
            status="in_progress", workspace_port=4200,
        )
        # Done → Port frei
        await make_task(
            board.id, title="Done 4201",
            status="done", workspace_port=4201,
        )
        # Aktiv → Port belegt
        await make_task(
            board.id, title="Active 4202",
            status="inbox", workspace_port=4202,
        )

        port = await _allocate_port(session)
        # 4200 belegt, 4201 frei (done), also 4201
        assert port == 4201


# ── API Integration (Port Release via PATCH) ─────────────────────────────

class TestPortReleaseOnStatusChange:

    @pytest.mark.asyncio
    async def test_port_released_on_done(
        self, auth_client, make_board, make_task,
    ):
        """Task auf done setzen → workspace_port wird None."""
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
        """Task auf failed setzen → workspace_port wird None."""
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
        """Task auf review setzen → workspace_port bleibt erhalten."""
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
