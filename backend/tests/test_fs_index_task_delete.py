"""Deleting a task whose file deliverable was captured into the files index.

``file_index.deliverable_id`` and ``file_index.task_id`` are bare NO-ACTION
foreign keys (migration 0129). Since the deliverable-create path captures an
index row at once, that row carries ``deliverable_id``. Both task-delete paths
(operator and agent) delete the task's ``TaskDeliverable`` rows, so they must
release the index row first — otherwise Postgres raises a
ForeignKeyViolation at commit and the delete answers 500.

The SQLite lane runs with foreign keys OFF, so here the same breakage shows
as a dangling ``deliverable_id`` left in the index; the ``postgres``-marked
twins run the real constraint on the Postgres lane.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.deliverable import TaskDeliverable
from app.models.file_index import FileIndexEntry
from app.services import file_indexer
from tests.conftest import test_engine


def _factory() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


async def _seed(scopes: list[str]):
    """Board, agent and task — committed one by one (FK order on Postgres)."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    raw, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(), name="Writer", board_id=board.id, agent_token_hash=token_hash,
        agent_runtime="cli-bridge", scopes=scopes,
    )
    task = Task(id=uuid.uuid4(), board_id=board.id, title="T", status="in_progress", assigned_agent_id=agent.id)
    for row in (board, agent, task):
        async with _factory() as s:
            s.add(row)
            await s.commit()
    return board.id, task.id, raw


async def _capture_then_delete(client: AsyncClient, tmp_path, monkeypatch, *, via: str) -> None:
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    monkeypatch.setattr(file_indexer, "async_session_maker", _factory)
    scopes = ["tasks:read", "tasks:write", "tasks:manage", "knowledge:read", "knowledge:write"]
    board_id, task_id, raw = await _seed(scopes)
    agent_headers = {"Authorization": f"Bearer {raw}"}

    f = tmp_path / ".mc" / "deliverables" / str(task_id) / "report.pdf"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("%PDF-1.4 hello")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
            json={"deliverable_type": "file", "title": "Report", "path": str(f)},
            headers=agent_headers,
        )
    assert resp.status_code == 201, resp.text
    deliverable_id = uuid.UUID(resp.json()["id"])

    async with _factory() as s:
        row = (await s.exec(select(FileIndexEntry).where(FileIndexEntry.deliverable_id == deliverable_id))).first()
    assert row is not None, "precondition: the capture wrote an index row with deliverable_id"

    if via == "operator":
        rd = await client.delete(f"/api/v1/boards/{board_id}/tasks/{task_id}")
    else:
        rd = await client.delete(f"/api/v1/agent/boards/{board_id}/tasks/{task_id}", headers=agent_headers)
    assert rd.status_code == 204, rd.text

    async with _factory() as s:
        gone = await s.get(TaskDeliverable, deliverable_id)
        dangling = (
            await s.exec(select(FileIndexEntry).where(FileIndexEntry.deliverable_id == deliverable_id))
        ).all()
        by_task = (await s.exec(select(FileIndexEntry).where(FileIndexEntry.task_id == task_id))).all()
        kept = (
            await s.exec(
                select(FileIndexEntry).where(
                    FileIndexEntry.root_key == "deliverables",
                    FileIndexEntry.rel_path == f"{task_id}/report.pdf",
                )
            )
        ).all()
    assert gone is None
    assert dangling == [], "file_index still points at the deleted deliverable"
    assert by_task == [], "file_index still points at the deleted task"
    assert len(kept) == 1, "the index row itself stays — the file is still on disk"


async def test_operator_task_delete_releases_captured_deliverable(auth_client, tmp_path, monkeypatch):
    await _capture_then_delete(auth_client, tmp_path, monkeypatch, via="operator")


async def test_agent_task_delete_releases_captured_deliverable(auth_client, tmp_path, monkeypatch):
    await _capture_then_delete(auth_client, tmp_path, monkeypatch, via="agent")


# Postgres lane: the real NO-ACTION constraint (CI runs `-m postgres` only).

@pytest.mark.postgres
async def test_operator_task_delete_releases_captured_deliverable_pg(auth_client, tmp_path, monkeypatch):
    await _capture_then_delete(auth_client, tmp_path, monkeypatch, via="operator")


@pytest.mark.postgres
async def test_agent_task_delete_releases_captured_deliverable_pg(auth_client, tmp_path, monkeypatch):
    await _capture_then_delete(auth_client, tmp_path, monkeypatch, via="agent")
