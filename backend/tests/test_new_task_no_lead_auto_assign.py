"""ADR-085 §4/§5: a task created from the UI/API WITHOUT an agent must not be
auto-assigned to the board lead any more.

Before: POST /boards/{id}/tasks (no assigned_agent_id) on a board with
auto_dispatch_enabled → auto_dispatch_task → find_dispatch_target picked the
board lead, prepared its repo workspace and enqueued the card in its Redis
queue — the old fleet path. In quiet mode (ADR-085 §5) new work goes to a
short-lived head, not to a persistent agent.

The switch is `settings.lead_auto_assign_new_tasks` (default False). Explicit
assignments keep dispatching exactly as before; True restores the old path.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task

from tests.conftest import test_engine


async def _board_with_lead() -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="NA", slug=f"na-{uuid.uuid4().hex[:6]}",
                      auto_dispatch_enabled=True)
        s.add(board)
        await s.commit()
        lead = Agent(id=uuid.uuid4(), name="Lead", role="Lead", board_id=board.id,
                     agent_runtime="host", model="x", is_board_lead=True)
        s.add(lead)
        await s.commit()
    return board.id, lead.id


def test_setting_defaults_to_off():
    from app.config import Settings

    assert Settings.model_fields["lead_auto_assign_new_tasks"].default is False


@pytest.mark.asyncio
async def test_unassigned_ui_task_is_not_dispatched_to_lead(auth_client: AsyncClient):
    board_id, _lead_id = await _board_with_lead()

    with patch("app.routers.tasks.auto_dispatch_task", new=AsyncMock()) as adt:
        r = await auth_client.post(f"/api/v1/boards/{board_id}/tasks",
                                   json={"title": "Neue Aufgabe ohne Agent"})
    assert r.status_code == 201, r.text
    adt.assert_not_called()

    task_id = uuid.UUID(r.json()["id"])
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        assert t.assigned_agent_id is None
        assert t.status == "inbox"
        ev = (await s.exec(select(ActivityEvent).where(
            ActivityEvent.task_id == task_id,
            ActivityEvent.event_type == "task.left_unassigned",
        ))).first()
        assert ev is not None, "skip must be visible in the activity feed"


@pytest.mark.asyncio
async def test_explicitly_assigned_ui_task_still_dispatches(auth_client: AsyncClient):
    board_id, lead_id = await _board_with_lead()

    with patch("app.routers.tasks.auto_dispatch_task", new=AsyncMock()) as adt:
        r = await auth_client.post(f"/api/v1/boards/{board_id}/tasks", json={
            "title": "Explizit zugewiesen", "assigned_agent_id": str(lead_id),
        })
    assert r.status_code == 201, r.text
    adt.assert_called_once()


@pytest.mark.asyncio
async def test_setting_on_restores_lead_auto_assign(auth_client: AsyncClient, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "lead_auto_assign_new_tasks", True)
    board_id, _ = await _board_with_lead()

    with patch("app.routers.tasks.auto_dispatch_task", new=AsyncMock()) as adt:
        r = await auth_client.post(f"/api/v1/boards/{board_id}/tasks",
                                   json={"title": "Alter Weg"})
    assert r.status_code == 201, r.text
    adt.assert_called_once()


@pytest.mark.asyncio
async def test_deferred_dispatch_of_unassigned_task_is_skipped(auth_client: AsyncClient):
    """The UI's reference-upload flow (defer_dispatch → POST .../dispatch) is the
    second door to the same lead auto-assign — it must be closed too."""
    board_id, _ = await _board_with_lead()

    with patch("app.routers.tasks.auto_dispatch_task", new=AsyncMock()) as adt:
        r = await auth_client.post(f"/api/v1/boards/{board_id}/tasks", json={
            "title": "Mit Referenzen", "defer_dispatch": True,
        })
        assert r.status_code == 201, r.text
        task_id = r.json()["id"]
        r2 = await auth_client.post(f"/api/v1/boards/{board_id}/tasks/{task_id}/dispatch")
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "left_unassigned"
    adt.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_dispatch_of_assigned_task_still_dispatches(auth_client: AsyncClient):
    board_id, lead_id = await _board_with_lead()

    with patch("app.routers.tasks.auto_dispatch_task", new=AsyncMock()) as adt:
        r = await auth_client.post(f"/api/v1/boards/{board_id}/tasks", json={
            "title": "Mit Referenzen", "defer_dispatch": True,
            "assigned_agent_id": str(lead_id),
        })
        task_id = r.json()["id"]
        r2 = await auth_client.post(f"/api/v1/boards/{board_id}/tasks/{task_id}/dispatch")
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "dispatch_triggered"
    adt.assert_called_once()
