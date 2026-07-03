"""Tests for dispatch race conditions (REL-02..04 + TST-02).

Three deterministic tests covering the known races:
  1. test_concurrent_dispatch_same_agent_queues  (REL-02)
  2. test_ack_while_reassignment_pending          (REL-03)
  3. test_redispatch_after_rejection_clears_old_dispatch  (REL-04)

Determinism: asyncio.Event as barrier, NO asyncio.sleep in test code
(CONTEXT.md D-05). Mock pattern: test_subagent_dispatch.py:22-33
(with patch("app.services.dispatch.rpc"), with patch("app.services.dispatch.engine", test_engine)).

Phase 1 Plan 02: STUBS ONLY — bodies get implemented in Plan 06.
Until then xfail. CI stays green because xfail is an expected failure.
"""
import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest



# Phase 29 / Gateway-Sunset: REL-02 and REL-04 race tests removed.
# They mocked `app.services.dispatch.rpc.chat_send` + `chat_send_isolated`
# which no longer exist. Equivalent coverage now lives in the no-RPC
# dispatch path (auto_dispatch_task → dispatch_delivery.py per-runtime
# branches). REL-03 (test_ack_while_reassignment_pending) is preserved
# below because it tests _check_dispatch_ack — gateway-independent.

async def test_ack_while_reassignment_pending(
    client, fake_redis, make_board, make_agent, make_task,
):
    """ACK arrives while approval escalation is running → invariant: no double
    dispatch, ack_at gets set, assigned_agent_id stays consistent.

    Production reality (see task_runner._handle_ack_timeout):
      - On ACK timeout, auto-reassign + chat_send are NO LONGER done;
        instead an approval is created (the operator decides manually).
      - Cooldown key (mc:dispatch:ack_check:{task_id}) prevents double approvals.
      - REL-03 verifies: if the agent ACKs (status: in_progress + ack_at) while
        the _create_dispatch_approval call is in flight, assigned_agent_id stays
        unchanged AND the ACK wins the race.
    """
    from datetime import timedelta
    from app.utils import utcnow
    from sqlmodel.ext.asyncio.session import AsyncSession
    from app.models.task import Task
    from app.services.task_runner import TaskRunnerService, task_runner

    board = await make_board(name="Race", slug="race", auto_dispatch_enabled=True)
    cody = await make_agent(
        name="Cody", board_id=board.id, is_board_lead=False,         scopes=["tasks:read", "tasks:write"],
    )
    # Backdate dispatched_at well past the ACK timeout (default 15min for openclaw).
    old_dispatch = utcnow() - timedelta(minutes=30)
    task = await make_task(
        board_id=board.id, status="inbox", title="Stale dispatch",
        assigned_agent_id=cody.id, dispatched_at=old_dispatch,
    )

    inside_approval_create = asyncio.Event()
    release_approval_create = asyncio.Event()

    real_create_approval = TaskRunnerService._create_dispatch_approval

    async def slow_create_approval(self, *args, **kwargs):
        inside_approval_create.set()
        await release_approval_create.wait()
        return await real_create_approval(self, *args, **kwargs)

    from tests.conftest import test_engine
    with patch("app.services.task_runner.get_redis", return_value=fake_redis), \
         patch.object(TaskRunnerService, "_create_dispatch_approval", slow_create_approval), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):

        async def _run_check():
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                # skip_pending=False → the ACK-timeout path is also evaluated
                await task_runner._check_dispatch_ack(s, skip_pending=False)

        # Spawn the escalation; it'll enter _create_dispatch_approval and park.
        t_runner = asyncio.create_task(_run_check())
        await inside_approval_create.wait()

        # Simulate ACK by direct DB mutation: status→in_progress, ack_at=now.
        # (Production path: PATCH /api/v1/agent/me/tasks/{id} status=in_progress.)
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            db_task = await s.get(Task, task.id)
            db_task.status = "in_progress"
            db_task.ack_at = utcnow()
            s.add(db_task)
            await s.commit()

        # Release escalation; let _create_dispatch_approval finish.
        release_approval_create.set()
        await t_runner

    # REL-03 invariants:
    #  1. ACK won — task is now in_progress, ack_at is set.
    #  2. assigned_agent_id stayed cody (no reassignment / flapping).
    #  3. At most ONE escalation Approval was created (cooldown protects).
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, task.id)
        assert refreshed.status == "in_progress", (
            f"ACK should win: status expected 'in_progress', got {refreshed.status!r}"
        )
        assert refreshed.ack_at is not None, "ACK must set ack_at"
        assert refreshed.assigned_agent_id == cody.id, (
            "assignment must stay with Cody (no reassignment race)"
        )
        from sqlmodel import select as _sel
        from app.models.approval import Approval
        approvals = (await s.exec(
            _sel(Approval).where(Approval.task_id == task.id)
        )).all()
        assert len(approvals) <= 1, (
            f"At most one escalation Approval expected, got {len(approvals)}"
        )

