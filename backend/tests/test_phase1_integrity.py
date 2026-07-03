"""Tests for Phase 1 Integrity Guards.

Step 1: owner_agent_id — Immutable Ownership Tracking
Step 2: Parent/Child Guard — parent done only when all children are done
Step 4: Self-Review Guard — agent may not approve their own code
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

# Standard patches for tests that succeed through to emit_event/broadcast
_BROADCAST_PATCH = patch("app.services.activity.broadcast", new_callable=AsyncMock)
_RPC_PATCH = patch("app.routers.agent_scoped.rpc", AsyncMock(connected=True), create=True)
# Phase 29: task_lifecycle.rpc removed; placeholder patch retained for backwards
# compat of `with _BROADCAST_PATCH, _LIFECYCLE_RPC_PATCH:` shape — patches the
# logger (innocuous attribute) instead.
_LIFECYCLE_RPC_PATCH = patch("app.services.task_lifecycle.logger", AsyncMock())

_REFLECTION_TEXT = (
    "## Was gemacht\nFeature fertig.\n"
    "## Was funktioniert\nAlles gruen.\n"
    "## Was unklar\nNichts.\n"
    "## Lesson\nTests vorher schreiben."
)


async def _add_reflection(task_id: uuid.UUID, agent_id: uuid.UUID):
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=agent_id,
            comment_type="reflection", content=_REFLECTION_TEXT,
        ))
        await s.commit()


async def _setup_integrity_scenario():
    """Create Board + Lead + Developer + Reviewer."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=board_id, name="Test Board", slug=f"test-{uuid.uuid4().hex[:8]}",
            require_review_before_done=True,
        )
        s.add(board)

        lead_token_raw, lead_token_hash = generate_agent_token()
        lead = Agent(
            id=lead_id, name="Henry", role="lead",
            board_id=board_id, agent_token_hash=lead_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:create", "tasks:manage"],
        )
        s.add(lead)

        dev_token_raw, dev_token_hash = generate_agent_token()
        developer = Agent(
            id=dev_id, name="Sparky", role="developer",
            board_id=board_id, agent_token_hash=dev_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
        )
        s.add(developer)

        reviewer_token_raw, reviewer_token_hash = generate_agent_token()
        reviewer = Agent(
            id=reviewer_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=reviewer_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(reviewer)

        await s.commit()

    return {
        "board_id": board_id,
        "lead_id": lead_id, "lead_token": lead_token_raw,
        "dev_id": dev_id, "dev_token": dev_token_raw,
        "reviewer_id": reviewer_id, "reviewer_token": reviewer_token_raw,
    }


# ────────────────────────────────────────────────────────────
# Step 1: owner_agent_id Tests
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_agent_id_set_on_agent_create(client):
    """Agent-created task gets owner_agent_id = creating agent."""
    ids = await _setup_integrity_scenario()

    with _BROADCAST_PATCH, _RPC_PATCH:
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks",
            headers={"Authorization": f"Bearer {ids['lead_token']}"},
            json={
                "title": "Test Task",
                "description": "A" * 60,
                "assigned_agent_id": str(ids["dev_id"]),
            },
        )

    assert resp.status_code == 201, f"Unexpected: {resp.status_code} {resp.text}"
    data = resp.json()
    assert data["owner_agent_id"] == str(ids["lead_id"]), (
        f"owner_agent_id should be lead {ids['lead_id']}, got {data.get('owner_agent_id')}"
    )


@pytest.mark.asyncio
async def test_owner_agent_id_null_on_manual_create(auth_client):
    """Manually created task (via dashboard) has owner_agent_id = null."""
    ids = await _setup_integrity_scenario()

    with _BROADCAST_PATCH:
        resp = await auth_client.post(
            f"/api/v1/boards/{ids['board_id']}/tasks",
            json={"title": "Manual Task"},
        )

    assert resp.status_code == 201, f"Unexpected: {resp.status_code} {resp.text}"
    data = resp.json()
    assert data.get("owner_agent_id") is None, (
        f"Manual task owner_agent_id should be null, got {data.get('owner_agent_id')}"
    )


# ────────────────────────────────────────────────────────────
# Step 2: Parent/Child Guard Tests
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parent_done_blocked_when_children_open(auth_client):
    """Parent cannot become done while children are still open."""
    ids = await _setup_integrity_scenario()
    from app.models.task import Task

    parent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = Task(
            id=parent_id, board_id=ids["board_id"],
            title="Parent", status="review",
            assigned_agent_id=ids["lead_id"],
        )
        child1 = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Child 1", status="done",
            parent_task_id=parent_id,
        )
        child2 = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Child 2", status="in_progress",
            parent_task_id=parent_id,
        )
        s.add_all([parent, child1, child2])
        await s.commit()

    resp = await auth_client.patch(
        f"/api/v1/boards/{ids['board_id']}/tasks/{parent_id}",
        json={"status": "done"},
    )

    assert resp.status_code == 400
    assert "Subtask(s) noch offen" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_parent_done_allowed_when_all_children_done(auth_client):
    """Parent can become done when all children are done."""
    ids = await _setup_integrity_scenario()
    from app.models.task import Task

    parent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = Task(
            id=parent_id, board_id=ids["board_id"],
            title="Parent", status="review",
            assigned_agent_id=ids["lead_id"],
        )
        child1 = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Child 1", status="done",
            parent_task_id=parent_id,
        )
        child2 = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Child 2", status="done",
            parent_task_id=parent_id,
        )
        s.add_all([parent, child1, child2])
        await s.commit()

    with _BROADCAST_PATCH:
        resp = await auth_client.patch(
            f"/api/v1/boards/{ids['board_id']}/tasks/{parent_id}",
            json={"status": "done"},
        )

    assert resp.status_code == 200, f"Unexpected: {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_parent_done_allowed_without_children(auth_client):
    """Task without children can go to done normally."""
    ids = await _setup_integrity_scenario()
    from app.models.task import Task

    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=task_id, board_id=ids["board_id"],
            title="Solo Task", status="review",
            assigned_agent_id=ids["lead_id"],
        )
        s.add(task)
        await s.commit()

    with _BROADCAST_PATCH:
        resp = await auth_client.patch(
            f"/api/v1/boards/{ids['board_id']}/tasks/{task_id}",
            json={"status": "done"},
        )

    assert resp.status_code == 200, f"Unexpected: {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_parent_done_blocked_via_agent(client):
    """Agent cannot set parent to done while children are open."""
    ids = await _setup_integrity_scenario()
    from app.models.task import Task

    parent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = Task(
            id=parent_id, board_id=ids["board_id"],
            title="Parent", status="in_progress",
            assigned_agent_id=ids["lead_id"],
        )
        child = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Child", status="blocked",
            parent_task_id=parent_id,
        )
        s.add_all([parent, child])
        await s.commit()

    resp = await client.patch(
        f"/api/v1/agent/boards/{ids['board_id']}/tasks/{parent_id}",
        headers={"Authorization": f"Bearer {ids['lead_token']}"},
        json={"status": "done"},
    )

    assert resp.status_code == 400
    assert "Subtask(s) noch offen" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_parent_done_blocked_mixed_children(auth_client):
    """Parent with done + failed children is blocked."""
    ids = await _setup_integrity_scenario()
    from app.models.task import Task

    parent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        parent = Task(
            id=parent_id, board_id=ids["board_id"],
            title="Parent", status="review",
            assigned_agent_id=ids["lead_id"],
        )
        child_done = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Child Done", status="done",
            parent_task_id=parent_id,
        )
        child_failed = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Child Failed", status="failed",
            parent_task_id=parent_id,
        )
        s.add_all([parent, child_done, child_failed])
        await s.commit()

    resp = await auth_client.patch(
        f"/api/v1/boards/{ids['board_id']}/tasks/{parent_id}",
        json={"status": "done"},
    )

    assert resp.status_code == 400
    assert "Subtask(s) noch offen" in resp.json()["detail"]


# ────────────────────────────────────────────────────────────
# Step 4: Self-Review Guard Tests
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_self_review_blocked(client):
    """Agent who worked on the task is escalated to the board lead (not 409)."""
    ids = await _setup_integrity_scenario()
    from app.models.task import Task, TaskEvent
    from unittest.mock import patch, AsyncMock

    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=task_id, board_id=ids["board_id"],
            title="Self-Review Test", status="review",
            assigned_agent_id=ids["dev_id"],
        )
        event = TaskEvent(
            task_id=task_id,
            from_status="inbox", to_status="in_progress",
            changed_by="agent", agent_id=ids["dev_id"],
        )
        s.add_all([task, event])
        await s.commit()

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{task_id}/review",
            headers={"Authorization": f"Bearer {ids['dev_token']}"},
            json={"decision": "approve", "comment": "ship-ready"},
        )

    # Self-review is escalated to the board lead (200), not blocked (409)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code} {resp.text}"

    # Task must be re-assigned to the board lead
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.assigned_agent_id == ids["lead_id"], "Task sollte an Board Lead eskaliert sein"
        assert task.status == "review", "Task bleibt in review"


@pytest.mark.asyncio
async def test_cross_review_allowed(client):
    """Reviewer who did not work on the task is allowed to approve."""
    ids = await _setup_integrity_scenario()
    from app.models.task import Task, TaskEvent

    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=task_id, board_id=ids["board_id"],
            title="Cross-Review Test", status="review",
            assigned_agent_id=ids["reviewer_id"],
        )
        event = TaskEvent(
            task_id=task_id,
            from_status="inbox", to_status="in_progress",
            changed_by="agent", agent_id=ids["dev_id"],
        )
        s.add_all([task, event])
        await s.commit()

    with _BROADCAST_PATCH, _LIFECYCLE_RPC_PATCH:
        resp = await client.post(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{task_id}/review",
            headers={"Authorization": f"Bearer {ids['reviewer_token']}"},
            json={"decision": "approve", "comment": "ship-ready"},
        )

    assert resp.status_code != 409, f"Self-review should not trigger for cross-review: {resp.text}"


# ────────────────────────────────────────────────────────────
# report_back Auto-Sent Tests (removed 2026-04-22)
# ────────────────────────────────────────────────────────────
#
# The old auto-sent logic (`report_back_status = "sent"` when owner/lead
# sets done) was replaced by the hard gate in agent_scoped.py:
# `task.report_sent_to_telegram` is set ONLY by an explicit `mc telegram`
# call. `mc done` without the flag → 422. `mc failed` without
# the flag → auto-draft.
#
# See `tests/test_report_back_gate.py` for current coverage (12 scenarios).
