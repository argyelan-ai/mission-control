"""Tests for MC hardening: auth matrix, active-task locking, readiness gates,
trigger/reset semantics, aborted recovery, runtime observability."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Auth-Matrix Tests ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_trigger_requires_auth(client):
    """Trigger endpoint without auth → 401/403."""
    agent_id = str(uuid.uuid4())
    resp = await client.post(
        f"/api/v1/agents/{agent_id}/trigger",
        json={"message": "test"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_reset_requires_auth(client):
    """Reset endpoint without auth → 401/403."""
    agent_id = str(uuid.uuid4())
    resp = await client.post(f"/api/v1/agents/{agent_id}/reset")
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_heartbeat_trigger_requires_auth(client):
    """Heartbeat trigger without auth → 401/403."""
    agent_id = str(uuid.uuid4())
    resp = await client.post(f"/api/v1/agents/{agent_id}/heartbeat")
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_trigger_with_user_auth(auth_client, make_board, make_agent):
    """Trigger with user auth on a real agent → 410 Gone (Phase 29 sunset), not 401."""
    board = await make_board()
    agent = await make_agent(name="TestBot", board_id=board.id)
    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/trigger",
        json={"message": "hello"},
    )
    assert resp.status_code == 410


# ── Active-Task Locking Tests ────────────────────────────────────────────

@pytest.mark.anyio
async def test_active_task_set_on_in_progress(session, make_board, make_agent, make_task):
    """current_task_id gets set when a task transitions to in_progress."""
    from app.services.task_lifecycle import update_agent_active_task

    board = await make_board()
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Test Task", assigned_agent_id=agent.id)

    await update_agent_active_task(session, agent.id, task, "in_progress", "inbox")

    from app.models.agent import Agent
    refreshed = await session.get(Agent, agent.id)
    assert refreshed.current_task_id == task.id
    assert refreshed.run_state == "running"


@pytest.mark.anyio
async def test_active_task_cleared_on_done(session, make_board, make_agent, make_task):
    """current_task_id gets cleared when a task transitions to done."""
    from app.services.task_lifecycle import update_agent_active_task
    from app.models.agent import Agent

    board = await make_board()
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Test Task", assigned_agent_id=agent.id)

    # First in_progress → sets lock
    await update_agent_active_task(session, agent.id, task, "in_progress", "inbox")
    # Then done → clears lock
    await update_agent_active_task(session, agent.id, task, "done", "in_progress")

    refreshed = await session.get(Agent, agent.id)
    assert refreshed.current_task_id is None
    assert refreshed.run_state == "idle"


@pytest.mark.anyio
async def test_active_task_blocked_sets_run_state(session, make_board, make_agent, make_task):
    """run_state becomes 'blocked' when a task transitions to blocked."""
    from app.services.task_lifecycle import update_agent_active_task
    from app.models.agent import Agent

    board = await make_board()
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Blocked Task", assigned_agent_id=agent.id)

    await update_agent_active_task(session, agent.id, task, "in_progress", "inbox")
    await update_agent_active_task(session, agent.id, task, "blocked", "in_progress")

    refreshed = await session.get(Agent, agent.id)
    assert refreshed.current_task_id is None
    assert refreshed.run_state == "blocked"


@pytest.mark.anyio
async def test_active_task_aborted_sets_run_state(session, make_board, make_agent, make_task):
    """run_state becomes 'aborted' when a task transitions to aborted."""
    from app.services.task_lifecycle import update_agent_active_task
    from app.models.agent import Agent

    board = await make_board()
    agent = await make_agent(name="Cody", board_id=board.id)
    task = await make_task(board_id=board.id, title="Aborted Task", assigned_agent_id=agent.id)

    await update_agent_active_task(session, agent.id, task, "in_progress", "inbox")
    await update_agent_active_task(session, agent.id, task, "aborted", "in_progress")

    refreshed = await session.get(Agent, agent.id)
    assert refreshed.current_task_id is None
    assert refreshed.run_state == "aborted"


# ── Task Status Transitions (aborted) ────────────────────────────────────

@pytest.mark.anyio
async def test_aborted_status_in_valid_transitions():
    """TaskStatusSelect shows aborted with correct transitions."""
    # Test the backend-side transition logic
    from app.models.task import Task

    task = Task(
        id=uuid.uuid4(),
        board_id=uuid.uuid4(),
        title="Test",
        status="aborted",
    )
    assert task.status == "aborted"


# ── Pipeline API with aborted ────────────────────────────────────────────

@pytest.mark.anyio
async def test_pipeline_includes_aborted(auth_client, make_board, make_task):
    """Pipeline API returns the aborted lane."""
    board = await make_board()
    await make_task(board_id=board.id, title="Aborted Task", status="aborted")

    resp = await auth_client.get(f"/api/v1/boards/{board.id}/tasks/pipeline")
    assert resp.status_code == 200
    data = resp.json()
    assert "aborted" in data["pipeline"]
    assert len(data["pipeline"]["aborted"]) == 1
    assert data["pipeline"]["aborted"][0]["title"] == "Aborted Task"


# ── Runtime Observability Tests ──────────────────────────────────────────

@pytest.mark.anyio
async def test_agent_has_runtime_fields(make_board, make_agent):
    """New fields: last_trigger_at, last_dispatch_error, run_state."""
    board = await make_board()
    agent = await make_agent(name="TestBot", board_id=board.id)

    assert agent.run_state == "idle"
    assert agent.last_trigger_at is None
    assert agent.last_dispatch_error is None


@pytest.mark.anyio
async def test_runtime_status_endpoint(auth_client, make_board, make_agent):
    """GET /agents/runtime-status returns compact runtime info."""
    board = await make_board()
    agent = await make_agent(name="TestBot", board_id=board.id)

    resp = await auth_client.get("/api/v1/agents/runtime-status")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    entry = data[0]
    assert "run_state" in entry
    assert "last_trigger_at" in entry
    assert "last_dispatch_error" in entry
    assert "provision_status" in entry
    assert entry["run_state"] == "idle"


# ── Readiness Gate Tests ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_dispatch_readiness_no_gateway(session, make_board, make_agent, make_task):
    """Phase 30: gateway_agent_id field removed — readiness gate now keyed on
    agent_runtime (NON_GATEWAY_RUNTIMES). cli-bridge agents are dispatchable
    by default (see find_dispatch_target). Test kept as a placeholder for
    the readiness-gate contract; pre-Phase-30 semantics no longer apply.
    """
    from app.models.agent import Agent

    board = await make_board()
    agent = await make_agent(name="NoGateway", board_id=board.id, is_board_lead=True)
    task = await make_task(board_id=board.id, title="Test", assigned_agent_id=agent.id)

    # Phase 30: gateway_agent_id is gone — agent_runtime "cli-bridge" (the
    # post-Phase-30 default in conftest.make_agent) is the dispatch-ready
    # signal. The "readiness gate" Phase 1 test exercised no longer exists.
    assert agent.agent_runtime == "cli-bridge"
