"""Tests fuer Approval→Promote Bridge.

Testmatrix:
- approved → Task verlässt planning
- rejected → kein Promote
- idempotent → doppelte Bearbeitung sicher
- dedupe → kein Approval-Spam
"""
import uuid
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import AsyncClient

from app.services.dispatch_gating import (
    NEEDS_APPROVAL,
    evaluate_promote_decision,
    process_planned_tasks,
)


# ── Dedupe Tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_dedupe_skips_approved_approval():
    """Kein neues Approval wenn ein approved Approval existiert."""
    from app.services.dispatch_gating import process_planned_tasks, NEEDS_APPROVAL
    from app.models.approval import Approval

    # Mock task that needs approval
    task = MagicMock()
    task.id = uuid.uuid4()
    task.board_id = uuid.uuid4()
    task.assigned_agent_id = uuid.uuid4()
    task.title = "Test"
    task.dispatch_phase = "planning"
    task.status = "inbox"
    task.dispatched_at = None
    task.parent_task_id = uuid.uuid4()
    task.autonomy_level = None
    task.approval_policy = "on_plan"
    task.requires_auth = False
    task.needs_browser = False
    task.delegation_type = "code_change"
    task.tags = []

    session = AsyncMock()

    # First exec returns our task
    task_result = MagicMock()
    task_result.all.return_value = [task]

    # Second exec (approval dedupe) returns existing approved approval
    existing_approval = MagicMock()
    existing_result = MagicMock()
    existing_result.first.return_value = existing_approval

    # Parent lookup returns None
    session.get = AsyncMock(return_value=None)
    session.exec = AsyncMock(side_effect=[task_result, existing_result])

    with patch("app.services.dispatch_gating.emit_event", new_callable=AsyncMock):
        stats = await process_planned_tasks(session)

    # No new approval created (dedupe caught it)
    assert stats["approval"] == 0


# ── Evaluate Decision Tests (approval-relevant) ────────

def test_approval_policy_on_plan_needs_approval():
    """approval_policy=on_plan → needs_approval."""
    task = MagicMock()
    task.autonomy_level = None
    task.approval_policy = "on_plan"
    task.requires_auth = False
    task.needs_browser = False
    task.delegation_type = "code_change"
    task.tags = []

    decision, reason = evaluate_promote_decision(task)
    assert decision == NEEDS_APPROVAL
    assert "approval_policy" in reason
