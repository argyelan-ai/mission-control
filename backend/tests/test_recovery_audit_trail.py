"""Recovery must leave a dispatch_attempt_id audit trail.

`task_attempt_audit` declares itself the Single Source of Truth for every
set/clear of `tasks.dispatch_attempt_id`, written *exclusively* via
`services.dispatch_attempt_audit` (models/task_attempt_audit.py:4). The
recovery endpoint assigned the field directly instead, so recovery-driven
rotations left no trace at all — 158 recoveries in the live DB produced zero
audit rows, and none of the nine recorded `caller` values was a recovery.

That is exactly the trail you need when reconstructing why a later update was
rejected as stale: without it, "the agent wrote from stale context" and "the
task was legitimately re-dispatched" are indistinguishable after the fact.
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.task_attempt_audit import TaskAttemptAudit


async def _agent_with_active_task(async_session: AsyncSession, *, attempt_id: str | None):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Rex-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        board_id=board.id,
        scopes=["tasks:read", "tasks:write"],
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    now = dt.datetime.now(tz=dt.timezone.utc)
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title="Wiederaufnahme",
        status="in_progress",
        dispatched_at=now,
        ack_at=now,
        dispatch_attempt_id=attempt_id,
    )
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    return board, agent, raw_token, task


async def _audit_rows(async_session: AsyncSession, task_id) -> list[TaskAttemptAudit]:
    return list(
        (
            await async_session.exec(
                select(TaskAttemptAudit)
                .where(TaskAttemptAudit.task_id == task_id)
                .order_by(TaskAttemptAudit.created_at)
            )
        ).all()
    )


async def _recover(client: AsyncClient, token: str):
    return await client.get(
        "/api/v1/agent/me/active-task-recovery",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_minting_a_fresh_attempt_id_is_audited(client: AsyncClient, async_session):
    """A task with no attempt id yet gets one minted during recovery — that is
    a real attempt-id change and belongs in the audit trail."""
    board, agent, token, task = await _agent_with_active_task(async_session, attempt_id=None)
    assert await _audit_rows(async_session, task.id) == []

    resp = await _recover(client, token)
    assert resp.status_code == 200
    assert resp.json()["active"] is True

    rows = await _audit_rows(async_session, task.id)
    assert len(rows) == 1
    assert rows[0].caller == "agent_recovery"
    assert rows[0].old_attempt is None
    assert str(rows[0].new_attempt) == resp.json()["task"]["dispatch_attempt_id"]


@pytest.mark.asyncio
async def test_reusing_an_existing_attempt_id_writes_no_audit_row(
    client: AsyncClient, async_session
):
    """Recovery deliberately reuses an existing attempt id (race fix
    2026-05-12) — nothing changes, so nothing may be audited. An audit row per
    recovery call would drown the real rotations."""
    existing = str(uuid.uuid4())
    board, agent, token, task = await _agent_with_active_task(async_session, attempt_id=existing)

    resp = await _recover(client, token)
    assert resp.status_code == 200
    assert resp.json()["task"]["dispatch_attempt_id"] == existing

    assert await _audit_rows(async_session, task.id) == []


@pytest.mark.asyncio
async def test_recovery_without_active_task_writes_nothing(client: AsyncClient, async_session):
    board, agent, token, task = await _agent_with_active_task(async_session, attempt_id=None)
    task.status = "done"
    async_session.add(task)
    await async_session.commit()

    resp = await _recover(client, token)
    assert resp.status_code == 200
    assert resp.json()["active"] is False
    assert await _audit_rows(async_session, task.id) == []
