"""Tests for auto-unassign on status transition to failed/blocked.

Bug background (2026-04-23): When a task is set to failed/blocked without
clearing assigned_agent_id, the agent gets stuck in a silent cancel loop:
agent_poll FIRST checks whether there's a failed task for the agent →
returns state="cancelled" → poll.sh sends ESC → next poll: same response.
Endless. New tasks are NEVER delivered because the failed task always
takes precedence.

Fix: central helper apply_terminal_unassign() that sets assigned_agent_id
to NULL on every transition to failed/blocked. Exception: blocked with
blocked_by_task_id (callback wait) — the parent agent must stay assigned
so the resume can route back.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.auth import generate_agent_token
from app.services.task_lifecycle import apply_terminal_unassign

from .conftest import test_engine


# ── Unit tests for apply_terminal_unassign ──────────────────────────────


@pytest.mark.asyncio
async def test_apply_terminal_unassign_failed_clears_assignment():
    """Transition → failed: assigned_agent_id is set to None."""
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b1")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id, current_task_id=task_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

        changed = await apply_terminal_unassign(s, task, "failed")
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    assert changed is True
    assert task.assigned_agent_id is None
    assert agent.current_task_id is None


@pytest.mark.asyncio
async def test_apply_terminal_unassign_blocked_without_callback_preserves_assignment():
    """Transition → blocked WITHOUT blocked_by_task_id: assigned_agent_id STAYS intact.

    Changed on 2026-04-24 (PR #111): Previously assigned_agent_id was cleared,
    which caused the task to escalate to the board lead instead of back to the
    original worker after mc blocked + operator approval. Now:
    - Task.assigned_agent_id stays (worker gets the task back on resume)
    - Agent.current_task_id is released (lock) so the worker can pick up other tasks
    - Agent.run_state → "blocked" for info
    """
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b2")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id, current_task_id=task_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
            blocked_by_task_id=None,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

        changed = await apply_terminal_unassign(s, task, "blocked")
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    # Post-PR-#111: assignment stays, only the lock is released
    assert changed is False, "blocked darf nicht mehr unassignen"
    assert task.assigned_agent_id == agent_id, "Worker bleibt assigned fuer Resume"
    assert agent.current_task_id is None, "Lock wird trotzdem freigegeben"
    # run_state change only happens if the old state was "running" or None
    # (otherwise idle/offline stays unchanged). In this test we had no explicit
    # run_state, default is "idle" in agents.py — no change expected.


@pytest.mark.asyncio
async def test_apply_terminal_unassign_blocked_with_callback_keeps_assignment():
    """Transition → blocked WITH blocked_by_task_id: assigned_agent_id STAYS.

    Structural callback wait (help_request, delegate). The parent agent
    must stay assigned, otherwise the resume after subtask-done can't
    route back to the right agent.
    """
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    sub_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b3")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id, current_task_id=task_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
            blocked_by_task_id=sub_id,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

        changed = await apply_terminal_unassign(s, task, "blocked")
        await s.commit()
        await s.refresh(task)
        await s.refresh(agent)

    assert changed is False, "Callback-Wait darf nicht unassignen"
    assert task.assigned_agent_id == agent_id
    # current_task_id must not be touched here (worker keeps working
    # while the subtask is pending)
    assert agent.current_task_id == task_id


@pytest.mark.asyncio
async def test_apply_terminal_unassign_no_op_for_other_status():
    """Transition → done/in_progress/review: no action."""
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b4")
        agent = Agent(id=agent_id, name="Worker", board_id=board_id)
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=agent_id,
        )
        s.add_all([board, agent, task])
        await s.commit()
        await s.refresh(task)

        for new_status in ("done", "in_progress", "review", "inbox", "user_test"):
            changed = await apply_terminal_unassign(s, task, new_status)
            assert changed is False, f"{new_status} darf nichts aendern"
            assert task.assigned_agent_id == agent_id


@pytest.mark.asyncio
async def test_apply_terminal_unassign_already_unassigned_safe():
    """Defensive: if assigned_agent_id is already None, no crash."""
    board_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="B", slug="b5")
        task = Task(
            id=task_id, board_id=board_id, title="T",
            status="in_progress", assigned_agent_id=None,
        )
        s.add_all([board, task])
        await s.commit()
        await s.refresh(task)

        # Watchdog already unassigned the task → helper must not break anything
        changed = await apply_terminal_unassign(s, task, "failed")
        assert changed is False
        assert task.assigned_agent_id is None


# ── Integration tests via PATCH endpoints ────────────────────────────────


async def _setup_basic(*, task_status: str = "in_progress", blocked_by: uuid.UUID | None = None):
    """Board + worker + task for integration tests."""
    board_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Bug Board", slug=f"bug-{uuid.uuid4().hex[:6]}")
        s.add(board)
        worker = Agent(
            id=worker_id,
            name="Worker",
            role="developer",
            board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            current_task_id=task_id,
        )
        s.add(worker)
        task = Task(
            id=task_id,
            board_id=board_id,
            title="Worker Task",
            status=task_status,
            assigned_agent_id=worker_id,
            blocked_by_task_id=blocked_by,
        )
        s.add(task)
        await s.commit()
        for o in (board, worker, task):
            await s.refresh(o)

    return {"board": board, "worker": worker, "task": task, "token": raw_token}


def _start_user_patch_mocks():
    """Start mocks for all external side effects during the user PATCH.
    Returns a list of patcher objects — caller must call stop() at the end.
    """
    mocks = [
        patch("app.routers.tasks.create_tracked_task"),
        patch("app.services.task_lifecycle.create_tracked_task", create=True),
        patch("app.services.auto_memory.create_tracked_task", create=True),
        patch("app.routers.tasks.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
    ]
    for m in mocks:
        m.start()
    return mocks


def _stop_mocks(mocks):
    for m in mocks:
        m.stop()


@pytest.mark.asyncio
async def test_user_patch_to_failed_auto_unassigns(auth_client):
    """User PATCH status: failed → assigned_agent_id is cleared.

    Reproduces the live bug: without auto-unassign the agent would get
    stuck in a cancel loop because agent_poll always prioritizes the
    failed task.
    """
    data = await _setup_basic(task_status="in_progress")

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "failed"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        worker = await s.get(Agent, data["worker"].id)

    assert task.status == "failed"
    assert task.assigned_agent_id is None, (
        "BUG: failed Task ohne unassign → Cancel-Schleife im agent_poll"
    )
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_user_patch_to_blocked_preserves_assignment(auth_client):
    """User PATCH status: blocked (without blocked_by_task_id) → assignment stays (PR #111).

    Previously (before 2026-04-24) assigned_agent_id was cleared. That caused
    worker orphaning on mc blocked: after operator resolution the task ended up
    with the board lead instead of back with the worker. Now assigned_agent_id
    stays intact; only the lock (current_task_id) is released.
    """
    data = await _setup_basic(task_status="in_progress")

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "blocked"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        worker = await s.get(Agent, data["worker"].id)

    assert task.status == "blocked"
    # Post-PR-#111: assignment stays, worker gets the task back on resume
    assert task.assigned_agent_id == data["worker"].id
    # Lock is still released so the worker can pick up other tasks
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_user_patch_to_blocked_with_callback_keeps_assignment(auth_client):
    """User PATCH status: blocked WITH blocked_by_task_id → assignment stays.

    Edge case: the operator manually sets a task to blocked that already has
    a subtask callback (e.g. after delegate). This is a structural wait
    state, NOT operator approval — must not unassign.
    """
    sub_id = uuid.uuid4()
    data = await _setup_basic(task_status="in_progress", blocked_by=sub_id)

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "blocked"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)

    assert task.status == "blocked"
    assert task.assigned_agent_id == data["worker"].id, (
        "Callback-Wait darf assignment nicht loeschen"
    )


@pytest.mark.asyncio
async def test_worker_patch_to_failed_auto_unassigns(client):
    """Worker PATCH status: failed (own task) → assigned is cleared."""
    data = await _setup_basic(task_status="in_progress")

    mocks = _start_user_patch_mocks()
    try:
        with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
            with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
                mock_rpc.connected = False
                mock_rpc.chat_send = AsyncMock()

                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "failed"},
                    headers={"Authorization": f"Bearer {data['token']}"},
                )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)
        worker = await s.get(Agent, data["worker"].id)

    assert task.status == "failed"
    assert task.assigned_agent_id is None
    assert worker.current_task_id is None


@pytest.mark.asyncio
async def test_worker_patch_to_blocked_with_callback_keeps_assignment(client):
    """Worker PATCH blocked + blocked_by_task_id (delegate pattern) → assignment stays."""
    sub_id = uuid.uuid4()
    data = await _setup_basic(task_status="in_progress")

    # Create subtask so blocked_by_task_id validation passes
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sub = Task(
            id=sub_id,
            board_id=data["board"].id,
            parent_task_id=data["task"].id,
            title="Sub",
            status="inbox",
            callback_agent_id=data["worker"].id,
        )
        s.add(sub)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.routers.agent_scoped.rpc", create=True) as mock_rpc:
            mock_rpc.connected = False
            mock_rpc.chat_send = AsyncMock()

            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={
                    "status": "blocked",
                    "blocked_by_task_id": str(sub_id),
                },
                headers={"Authorization": f"Bearer {data['token']}"},
            )

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, data["task"].id)

    assert task.status == "blocked"
    assert task.assigned_agent_id == data["worker"].id, (
        "Callback-Wait (delegate-Pattern) darf assignment nicht loeschen"
    )
    assert task.blocked_by_task_id == sub_id


@pytest.mark.asyncio
async def test_help_request_self_block_keeps_assignment(client):
    """help_request endpoint sets blocked_by_task_id → original agent stays assigned."""
    board_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    helper_id = uuid.uuid4()
    task_id = uuid.uuid4()

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="HR Board", slug=f"hr-{uuid.uuid4().hex[:6]}")
        s.add(board)
        requester = Agent(
            id=requester_id, name="Coder", role="developer", board_id=board_id,
            agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "tasks:help"],
            current_task_id=task_id,
            provision_status="provisioned",
        )
        helper = Agent(
            id=helper_id, name="Helper", role="developer", board_id=board_id,
            provision_status="provisioned",
        )
        s.add(requester)
        s.add(helper)
        task = Task(
            id=task_id, board_id=board_id, title="Coder Task",
            status="in_progress", assigned_agent_id=requester_id,
        )
        s.add(task)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/help-request",
            json={
                "title": "Need help with X",
                "context": "Stuck on Y",
                "needed_role": "developer",
            },
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    assert resp.status_code in (200, 201), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)

    assert task.status == "blocked"
    assert task.assigned_agent_id == requester_id, (
        "help_request: Original-Agent muss assigned bleiben (blocked_by_task_id Callback)"
    )
    assert task.blocked_by_task_id is not None


@pytest.mark.asyncio
async def test_user_patch_failed_does_not_break_already_unassigned(auth_client):
    """Defensive: watchdog already unassigned the task → user PATCH does not crash."""
    board_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="DB", slug=f"db-{uuid.uuid4().hex[:6]}")
        s.add(board)
        task = Task(
            id=task_id, board_id=board_id, title="Stale",
            status="in_progress", assigned_agent_id=None,  # already unassigned
        )
        s.add(task)
        await s.commit()

    mocks = _start_user_patch_mocks()
    try:
        # Phase 29 / Wave 4 cleanup: app.routers.tasks.rpc no longer exists.
        resp = await auth_client.patch(
            f"/api/v1/boards/{board_id}/tasks/{task_id}",
            json={"status": "failed"},
        )
    finally:
        _stop_mocks(mocks)

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)

    assert task.status == "failed"
    assert task.assigned_agent_id is None
