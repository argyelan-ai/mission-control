"""Tests for approval cleanup — task status supersedes obsolete approvals.

Lifecycle rules:
- blocker_decision: valid only when task.status == blocked
- spawn_timeout: valid only when task.status == inbox
- dispatch_escalation: valid only when task.status == inbox
- When task leaves the state → approval → superseded
"""
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine
from app.services.approval_cleanup import cleanup_obsolete_approvals, reconcile_stale_approvals


async def _create_task_with_approval(action_type: str, task_status: str = "blocked"):
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.approval import Approval

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    approval_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Cleanup Test", slug=f"cleanup-{uuid.uuid4().hex[:8]}"))
        s.add(Agent(id=agent_id, name="TestAgent", role="developer", board_id=board_id))
        s.add(Task(id=task_id, board_id=board_id, title="Cleanup Task", status=task_status, assigned_agent_id=agent_id))
        s.add(Approval(id=approval_id, board_id=board_id, task_id=task_id, agent_id=agent_id,
                        action_type=action_type, description="Test", status="pending"))
        await s.commit()

    return {"board_id": board_id, "agent_id": agent_id, "task_id": task_id, "approval_id": approval_id}


@pytest.mark.asyncio
async def test_blocker_decision_superseded_on_unblock():
    """blocker_decision → superseded when task is no longer blocked."""
    ids = await _create_task_with_approval("blocker_decision", task_status="blocked")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await cleanup_obsolete_approvals(s, ids["task_id"], "in_progress")
        assert count == 1

        from app.models.approval import Approval
        approval = await s.get(Approval, ids["approval_id"])
        assert approval.status == "superseded"
        assert "Superseded" in approval.resolver_note


@pytest.mark.asyncio
async def test_spawn_timeout_superseded_on_progress():
    """spawn_timeout → superseded when task is no longer inbox."""
    ids = await _create_task_with_approval("spawn_timeout", task_status="inbox")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await cleanup_obsolete_approvals(s, ids["task_id"], "in_progress")
        assert count == 1

        from app.models.approval import Approval
        approval = await s.get(Approval, ids["approval_id"])
        assert approval.status == "superseded"


@pytest.mark.asyncio
async def test_dispatch_escalation_superseded_on_ack():
    """dispatch_escalation → superseded when task is no longer inbox."""
    ids = await _create_task_with_approval("dispatch_escalation", task_status="inbox")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await cleanup_obsolete_approvals(s, ids["task_id"], "in_progress")
        assert count == 1

        from app.models.approval import Approval
        approval = await s.get(Approval, ids["approval_id"])
        assert approval.status == "superseded"


@pytest.mark.asyncio
async def test_blocker_stays_pending_while_blocked():
    """blocker_decision stays pending while task is still blocked."""
    ids = await _create_task_with_approval("blocker_decision", task_status="blocked")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await cleanup_obsolete_approvals(s, ids["task_id"], "blocked")
        assert count == 0

        from app.models.approval import Approval
        approval = await s.get(Approval, ids["approval_id"])
        assert approval.status == "pending"


@pytest.mark.asyncio
async def test_approved_not_overwritten():
    """An already approved approval is not accidentally overwritten."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.models.approval import Approval

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    approval_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="No Overwrite", slug=f"nooverwrite-{uuid.uuid4().hex[:8]}"))
        s.add(Agent(id=agent_id, name="Agent", role="developer", board_id=board_id))
        s.add(Task(id=task_id, board_id=board_id, title="Approved Task", status="in_progress", assigned_agent_id=agent_id))
        s.add(Approval(id=approval_id, board_id=board_id, task_id=task_id, agent_id=agent_id,
                        action_type="blocker_decision", description="Already resolved",
                        status="approved"))  # Already approved!
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await cleanup_obsolete_approvals(s, task_id, "in_progress", board_id)
        assert count == 0  # Do not touch!

        approval = await s.get(Approval, approval_id)
        assert approval.status == "approved"  # Stays approved


@pytest.mark.asyncio
async def test_watchdog_reconciliation():
    """Watchdog cleans up drifted approvals."""
    ids = await _create_task_with_approval("blocker_decision", task_status="blocked")

    # Manually set task to in_progress (simulates the drift)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        task = await s.get(Task, ids["task_id"])
        task.status = "in_progress"
        s.add(task)
        await s.commit()

    # Reconciliation should find the drift
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await reconcile_stale_approvals(s)
        assert count >= 1

        from app.models.approval import Approval
        approval = await s.get(Approval, ids["approval_id"])
        assert approval.status == "superseded"
        assert "reconciliation" in approval.resolver_note


@pytest.mark.asyncio
async def test_done_supersedes_all_flow_approvals():
    """Task set to done → all flow-related pending approvals superseded."""
    ids = await _create_task_with_approval("blocker_decision", task_status="blocked")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        count = await cleanup_obsolete_approvals(s, ids["task_id"], "done")
        assert count == 1

        from app.models.approval import Approval
        approval = await s.get(Approval, ids["approval_id"])
        assert approval.status == "superseded"
