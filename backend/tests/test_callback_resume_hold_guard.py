"""8th claim path (Rex, PR #533 counter-check, 2026-09-13):
_handle_callback_resume (agent_task_status.py) only checked
parent.status == "blocked" before resuming — never run_control.

Reproduced via real verbs, no hand-set run_control:
  1. Agent works the parent task (in_progress), earlier created a callback
     subtask (blocked_by_task_id set).
  2. Operator stops the running parent via stop_task_run — this lands the
     parent at status="blocked", run_control="stopped",
     blocked_by_task_id unchanged, assigned_agent_id retained (documented
     stop_task_run behavior).
  3. The callback subtask finishes.

Pre-fix: the callback fallback's only precondition (status=="blocked") is
satisfied, so it silently resumes the stopped parent to "in_progress" —
exactly the deadlock PR #533 closed five other paths for (mc release
doesn't undo it: it only accepts run_control=="manual_hold").
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_stopped_parent_not_resumed_by_callback(make_board, make_agent, make_task):
    """Parent stopped via run_control="stopped" while blocked_by_task_id
    still points at a callback subtask that then finishes — must NOT
    resume. Mirrors Rex's real-verb repro (stop_task_run's actual
    post-state), not a hand-crafted one."""
    from app.routers.agent_task_status import _handle_callback_resume

    board = await make_board(name="Callback Hold", slug="callback-hold")
    agent = await make_agent(name="Worker", board_id=board.id)

    parent = await make_task(
        board_id=board.id, title="Stopped mid-callback-wait",
        status="blocked", run_control="stopped",
        assigned_agent_id=agent.id,
    )
    subtask = await make_task(
        board_id=board.id, title="Callback subtask", status="done",
        parent_task_id=parent.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        p = await s.get(type(parent), parent.id)
        p.blocked_by_task_id = subtask.id
        s.add(p)
        await s.commit()

    with patch("app.routers.agent_task_status.dispatch_callback_to_parent", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.services.activity.emit_event", new_callable=AsyncMock) as mock_emit:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            sub = await s.get(type(subtask), subtask.id)
            await _handle_callback_resume(s, sub)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        p = await s.get(type(parent), parent.id)
        assert p.status == "blocked", p.status
        assert p.run_control == "stopped"
        assert p.blocked_by_task_id == subtask.id

    mock_dispatch.assert_not_called()
    mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_unheld_blocked_parent_still_resumed_by_callback(make_board, make_agent, make_task):
    """Counter-probe: a normally-blocked parent (run_control=None) whose
    callback subtask finishes must still resume exactly as before — the
    added guard must not overreach into the ordinary case."""
    from app.routers.agent_task_status import _handle_callback_resume

    board = await make_board(name="Callback OK", slug="callback-ok")
    agent = await make_agent(name="Worker2", board_id=board.id)

    parent = await make_task(
        board_id=board.id, title="Normally blocked, waiting on callback",
        status="blocked", run_control=None,
        assigned_agent_id=agent.id,
    )
    subtask = await make_task(
        board_id=board.id, title="Callback subtask 2", status="done",
        parent_task_id=parent.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        p = await s.get(type(parent), parent.id)
        p.blocked_by_task_id = subtask.id
        s.add(p)
        await s.commit()

    with patch("app.routers.agent_task_status.dispatch_callback_to_parent", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.services.activity.emit_event", new_callable=AsyncMock) as mock_emit:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            sub = await s.get(type(subtask), subtask.id)
            await _handle_callback_resume(s, sub)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        p = await s.get(type(parent), parent.id)
        assert p.status == "in_progress", p.status
        assert p.blocked_by_task_id is None

    mock_dispatch.assert_called_once()
    mock_emit.assert_called_once()
