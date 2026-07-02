"""Orchestrator-created subtasks must respect depends_on at initial dispatch.

Regression for the bug where a Board Lead creating a subtask via
POST /agent/boards/{board_id}/tasks with both `assigned_agent_id` and
`depends_on` would dispatch the subtask immediately via the direct
cli-bridge / gateway path, skipping the dependency check that
auto_dispatch / watchdog / task_lifecycle all honor.

Observed: Shakespeare Phase 2b (`depends_on: [Davinci Phase 1a]`) got
dispatched to in_progress while Davinci Phase 1a was still running.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskDependency
from tests.conftest import test_engine


async def _setup_orchestrator_and_worker(
    *, worker_runtime: str = "cli-bridge"
):
    """Create a board + board-lead + cli-bridge worker and return auth token."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Orc Board", slug=f"orc-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        raw_token, token_hash = generate_agent_token()
        lead = Agent(
            id=uuid.uuid4(),
            name=f"Boss-{uuid.uuid4().hex[:6]}",
            role="lead",
            board_id=board.id,
            is_board_lead=True,
            agent_token_hash=token_hash,
            agent_runtime="host",
        )
        worker = Agent(
            id=uuid.uuid4(),
            name=f"Worker-{uuid.uuid4().hex[:6]}",
            role="writer",
            board_id=board.id,
            agent_runtime=worker_runtime,
        )
        s.add(lead)
        s.add(worker)
        await s.commit()
        await s.refresh(lead)
        await s.refresh(worker)
        return board, lead, worker, raw_token


@pytest.mark.asyncio
async def test_orchestrator_subtask_waits_for_unfinished_dependency(client: AsyncClient):
    """Board Lead creates a subtask with depends_on pointing at an in_progress task.
    The subtask must stay in inbox, unscheduled, until the dependency is done.
    """
    board, lead, worker, lead_token = await _setup_orchestrator_and_worker()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dep_task = Task(
            id=uuid.uuid4(),
            board_id=board.id,
            title="Phase 1a (in progress)",
            status="in_progress",
            assigned_agent_id=worker.id,
        )
        s.add(dep_task)
        await s.commit()

    with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge",
               new_callable=AsyncMock) as mock_cli, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        mock_cli.return_value = True
        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={
                "title": "Phase 2b — Copy",
                "description": (
                    "Ziel: Copy fuer alle 7 Sektionen.\n"
                    "Kontext: Phase 1a produziert die Sektions-Liste.\n"
                    "Guardrails: kein Deploy.\n"
                    "Erwarteter Output: 7 Markdown-Abschnitte.\n"
                    "Definition of Done: reviewed."
                ),
                "assigned_agent_id": str(worker.id),
                "depends_on": [str(dep_task.id)],
            },
        )

    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    task_id = uuid.UUID(body["id"])

    assert mock_cli.await_count == 0, (
        "cli-bridge dispatch must NOT fire while a dependency is still open"
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        assert t is not None
        assert t.status == "inbox", f"expected inbox, got {t.status}"
        assert t.dispatched_at is None, "task must not be dispatched"
        assert t.ack_at is None, "task must not be ACK'd"

        deps = list((await s.exec(
            select(TaskDependency).where(TaskDependency.task_id == task_id)
        )).all())
        assert len(deps) == 1
        assert deps[0].depends_on_task_id == dep_task.id


@pytest.mark.asyncio
async def test_orchestrator_subtask_dispatches_when_dependency_done(client: AsyncClient):
    """Counter-case: if the dependency is already done, dispatch proceeds."""
    board, lead, worker, lead_token = await _setup_orchestrator_and_worker()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dep_task = Task(
            id=uuid.uuid4(),
            board_id=board.id,
            title="Phase 1a (done)",
            status="done",
        )
        s.add(dep_task)
        await s.commit()

    with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge",
               new_callable=AsyncMock) as mock_cli, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch._build_dispatch_message",
               new_callable=AsyncMock) as mock_msg:
        mock_cli.return_value = True
        mock_msg.return_value = "dispatch message"

        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={
                "title": "Phase 2b — Copy",
                "description": (
                    "Ziel: Copy fuer 7 Sektionen.\n"
                    "Kontext: Phase 1a ist done, Dispatch darf laufen.\n"
                    "Guardrails: keine.\n"
                    "Erwarteter Output: Markdown.\n"
                    "Definition of Done: Review."
                ),
                "assigned_agent_id": str(worker.id),
                "depends_on": [str(dep_task.id)],
            },
        )

    assert resp.status_code in (200, 201), resp.text
    task_id = uuid.UUID(resp.json()["id"])

    mock_cli.assert_awaited_once()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        assert t is not None
        assert t.dispatched_at is not None, "task should be dispatched"


# Phase 29 / Gateway-Sunset: test_orchestrator_subtask_waits_for_deps_also_on_gateway_path
# was removed — it patched app.services.dispatch.rpc.chat_send /
# chat_send_isolated which no longer exist. The dependency gate (
# `_skip_dispatch` in auto_dispatch_task) is covered by
# test_orchestrator_subtask_waits_for_unfinished_dependency (cli-bridge)
# above. There is no separate gateway path anymore.


@pytest.mark.asyncio
async def test_orchestrator_subtask_without_deps_still_dispatches(client: AsyncClient):
    """Guardrail: the new gate must not accidentally block deps-free tasks."""
    board, lead, worker, lead_token = await _setup_orchestrator_and_worker()

    with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge",
               new_callable=AsyncMock) as mock_cli, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch._build_dispatch_message",
               new_callable=AsyncMock) as mock_msg:
        mock_cli.return_value = True
        mock_msg.return_value = "dispatch message"

        resp = await client.post(
            f"/api/v1/agent/boards/{board.id}/tasks",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={
                "title": "Standalone Task",
                "description": (
                    "Ziel: Standalone-Task ohne Abhaengigkeit.\n"
                    "Kontext: Guardrail-Test fuer neuen Gate.\n"
                    "Guardrails: darf normal dispatchen.\n"
                    "Erwarteter Output: dispatched.\n"
                    "Definition of Done: Task ist dispatched."
                ),
                "assigned_agent_id": str(worker.id),
            },
        )

    assert resp.status_code in (200, 201), resp.text
    mock_cli.assert_awaited_once()
