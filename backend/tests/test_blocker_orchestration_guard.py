"""Tests: `status=blocked` approval guard for orchestration waits.

Incident context 2026-04-23: Boss without mc CLI on host made a manual curl
POST /tasks + PATCH status=blocked (anti-pattern). That did NOT set
blocked_by_task_id — Guard 1 in agent_scoped.py only triggers when
blocked_by_task_id is set.
Result: system creates a blocker_decision approval for the operator → inbox
spam + watchdog fallback blocked by pending approval → parent stuck.

Fix: Guard 2 — if the agent has at least one active child subtask with
callback_agent_id = the blocking agent, that's also an orchestration wait
and NO approval should be created.
"""

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_board_agent_task():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="BlockerGuard", slug=f"bg-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Orchestr", role="orchestrator",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write"],
            provision_status="provisioned",
            is_board_lead=True,
            current_task_id=task_id,
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Parent Orchestration Task",
            status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
        ))
        await s.commit()
    return board_id, agent_id, task_id, token_raw


@pytest.mark.asyncio
async def test_blocked_without_blocked_by_but_with_callback_subtask_skips_approval(client, fake_redis):
    """Primary test: agent does PATCH status=blocked without blocked_by_task_id,
    but an active child subtask with callback exists → NO approval."""
    from app.models.task import Task
    from app.models.approval import Approval
    from sqlmodel import select

    board_id, agent_id, task_id, token = await _setup_board_agent_task()

    # Create child subtask with callback (simulates the raw-curl anti-pattern)
    child_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        from app.auth import generate_agent_token
        _, worker_hash = generate_agent_token()
        s.add(Agent(
            id=worker_id, name="Worker", role="researcher",
            board_id=board_id, agent_token_hash=worker_hash,
            scopes=["tasks:read"],             provision_status="provisioned",
        ))
        s.add(Task(
            id=child_id, board_id=board_id, title="Child Research",
            status="in_progress",
            parent_task_id=task_id,
            assigned_agent_id=worker_id,
            callback_agent_id=agent_id,  # callback to orchestrator
        ))
        await s.commit()

    # Orchestrator blocks itself (WITHOUT blocked_by_task_id — anti-pattern)
    # Commenting first (block status requires a prior comment)
    await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"content": "Warte auf child", "comment_type": "blocker"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "blocked",
            "blocker_type": "dependency_blocked",
            "blocker_question": "Warte auf Research-Callback",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 409), resp.text  # 409 ok if dispatch_attempt_id check triggers

    # Key assertion: NO blocker_decision approval created
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = list((await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "blocker_decision",
            )
        )).all())
    assert len(approvals) == 0, (
        f"Guard 2 hat nicht gegriffen — {len(approvals)} Approval(s) erstellt "
        f"obwohl Child-Subtask {child_id} mit callback existiert."
    )


@pytest.mark.asyncio
async def test_blocked_without_any_callback_creates_approval(client, fake_redis):
    """Control test: agent blocks without blocked_by_task_id AND without a child
    callback → approval WILL be created (a real human-decision block)."""
    from app.models.approval import Approval
    from sqlmodel import select

    board_id, agent_id, task_id, token = await _setup_board_agent_task()

    # Blocker-comment first
    await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"content": "Brauche Entscheidung vom Operator", "comment_type": "blocker"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "blocked",
            "blocker_type": "decision_needed",
            "blocker_question": "Welche Option soll ich waehlen?",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 409), resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = list((await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "blocker_decision",
            )
        )).all())
    # Real blocker without callback → approval should still be created
    assert len(approvals) >= 1, (
        "Control-test fehlgeschlagen — Approval sollte existieren fuer echte Operator-Decision-Blocker"
    )


@pytest.mark.asyncio
async def test_blocked_with_done_child_still_creates_approval(client, fake_redis):
    """Edge case: child is `done` (not active) → no orchestration wait anymore,
    approval should be created when the parent agent blocks."""
    from app.models.task import Task
    from app.models.approval import Approval
    from sqlmodel import select

    board_id, agent_id, task_id, token = await _setup_board_agent_task()

    # Done child — no active callback wait
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=uuid.uuid4(), board_id=board_id, title="Done Child",
            status="done",
            parent_task_id=task_id,
            assigned_agent_id=agent_id,
            callback_agent_id=agent_id,
        ))
        await s.commit()

    await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        json={"content": "blocker", "comment_type": "blocker"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = await client.patch(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "blocked",
            "blocker_type": "other",
            "blocker_question": "Weiter wie?",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 409)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = list((await s.exec(
            select(Approval).where(
                Approval.task_id == task_id,
                Approval.action_type == "blocker_decision",
            )
        )).all())
    # Done child — Guard 2 does NOT trigger (no active callback subtasks)
    assert len(approvals) >= 1, (
        "Done-Child sollte nicht als orchestration-wait zaehlen — Approval muss erstellt werden"
    )
