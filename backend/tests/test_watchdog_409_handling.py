"""409-Handling im Watchdog (PR #478 Nacharbeit, Architektur C 3/3).

``lock_and_set()`` re-validates the status transition against a freshly
locked read and raises ``HTTPException(409)`` when another writer changed
the row between the sweep's own SELECT and this re-read — exactly the race
the row lock was built to catch. Before this fix, all four call sites in
``task_monitor.py`` ran ``lock_and_set()`` inside a ``for`` loop with no
``try/except``: one lost race raised out of the loop and aborted the whole
sweep, leaving every other candidate in that tick unprocessed.

One test per pattern (not per call site — see task description):
- immediate-commit sweep loop (``_check_phase_completions`` — same pattern
  as ``_check_stuck_orchestrator_close``, not duplicated here)
- second immediate-commit sweep loop, different candidate-selection logic
  (``_check_stuck_orchestrator_close``)
- deferred single-commit batch loop (``_recover_orphaned_tasks``)
- the nested, non-loop call site (``_auto_advance_next_phase``) whose fix
  also reorders ``lock_and_set()`` before ``record_task_event()`` so a lost
  race can't leave a phantom event behind
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.task import Task
from app.services.task_state import lock_and_set as real_lock_and_set
from app.services.watchdog.task_monitor import TaskMonitorMixin
from app.utils import utcnow
from tests.conftest import test_engine
from sqlmodel.ext.asyncio.session import AsyncSession


def _flaky_lock_and_set(fail_task_id: uuid.UUID):
    """Real lock_and_set, but 409s for one specific task_id — simulates the
    row's status having changed between the sweep's SELECT and this call."""

    async def _inner(session, task_id, to, *, actor):
        if task_id == fail_task_id:
            raise HTTPException(status_code=409, detail="Ungueltiger Statuswechsel (simuliert)")
        return await real_lock_and_set(session, task_id, to, actor=actor)

    return _inner


async def _make_standalone_parent(session, board_id, title, *, updated_at=None):
    parent = Task(
        id=uuid.uuid4(), board_id=board_id, title=title,
        status="in_progress", parent_task_id=None,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)
    if updated_at is not None:
        parent.updated_at = updated_at
        session.add(parent)
        await session.commit()
        await session.refresh(parent)
    return parent


async def _make_done_subtask(session, board_id, parent_id, title, **kwargs):
    task = Task(
        id=uuid.uuid4(), board_id=board_id, parent_task_id=parent_id,
        title=title, status="done", **kwargs,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_check_phase_completions_409_skips_task_continues_sweep(
    mock_emit, session, make_board,
):
    """Two standalone parents, both phase-complete, no Board Lead (fallback
    Rex-handoff path, call site ~line 202). Parent A loses the lock_and_set
    race (409) — the sweep must still process parent B in the same tick."""
    board = await make_board(name="409 Board A", slug="409-board-a")

    parent_a = await _make_standalone_parent(session, board.id, "Parent A (races)")
    await _make_done_subtask(session, board.id, parent_a.id, "Sub A.1")

    parent_b = await _make_standalone_parent(session, board.id, "Parent B (fine)")
    await _make_done_subtask(session, board.id, parent_b.id, "Sub B.1")

    monitor = TaskMonitorMixin()
    with patch(
        "app.services.watchdog.task_monitor.lock_and_set",
        side_effect=_flaky_lock_and_set(parent_a.id),
    ), patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
        await monitor._check_phase_completions(session)  # must not raise

    await session.refresh(parent_a)
    await session.refresh(parent_b)

    # Lost the race → untouched, retried next tick.
    assert parent_a.status == "in_progress"
    # Sweep continued past the 409 → parent B still gets processed.
    assert parent_b.status == "review"


@pytest.mark.asyncio
@patch("app.services.task_lifecycle._escalate_orch_close_to_mark", new_callable=AsyncMock)
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_check_stuck_orchestrator_close_409_skips_task_continues_sweep(
    mock_emit, mock_escalate, session, make_board, make_agent, isolate_redis_singleton,
):
    """Two stuck parents past the 2-nudge auto-close threshold (call site
    ~line 398). Parent A loses the lock_and_set race — the sweep must still
    auto-close parent B in the same tick."""
    board = await make_board(name="409 Board B", slug="409-board-b")
    lead = await make_agent(name="Lead", board_id=board.id, is_board_lead=True)

    stale = utcnow() - timedelta(minutes=10)
    parent_a = await _make_standalone_parent(session, board.id, "Stuck A (races)", updated_at=stale)
    await _make_done_subtask(
        session, board.id, parent_a.id, "Approval A", delegation_type="phase_approval",
    )
    parent_b = await _make_standalone_parent(session, board.id, "Stuck B (fine)", updated_at=stale)
    await _make_done_subtask(
        session, board.id, parent_b.id, "Approval B", delegation_type="phase_approval",
    )

    redis = isolate_redis_singleton
    await redis.set(f"mc:watchdog:stuck_orch_close_count:{parent_a.id}", "2")
    await redis.set(f"mc:watchdog:stuck_orch_close_count:{parent_b.id}", "2")

    monitor = TaskMonitorMixin()
    with patch(
        "app.services.watchdog.task_monitor.lock_and_set",
        side_effect=_flaky_lock_and_set(parent_a.id),
    ):
        await monitor._check_stuck_orchestrator_close(session)  # must not raise

    await session.refresh(parent_a)
    await session.refresh(parent_b)

    assert parent_a.status == "in_progress"
    assert parent_b.status == "review"


@pytest.mark.asyncio
async def test_recover_orphaned_tasks_409_skips_task_batch_commit_continues(
    session, make_board, make_agent,
):
    """Two orphaned tasks (stale in_progress, dead agent) with a single
    batched commit at the end of the loop (call site ~line 1348). Task A
    loses the lock_and_set race — the batch must still recover + commit
    task B, and the returned count must only reflect actual successes."""
    board = await make_board(name="409 Board C", slug="409-board-c")
    dead_agent = await make_agent(name="Dead Agent", board_id=board.id)

    old = utcnow() - timedelta(minutes=45)
    task_a = Task(
        id=uuid.uuid4(), board_id=board.id, title="Orphan A (races)",
        status="in_progress", assigned_agent_id=dead_agent.id, updated_at=old,
    )
    task_b = Task(
        id=uuid.uuid4(), board_id=board.id, title="Orphan B (fine)",
        status="in_progress", assigned_agent_id=dead_agent.id, updated_at=old,
    )
    session.add_all([task_a, task_b])
    await session.commit()

    monitor = TaskMonitorMixin()
    with patch(
        "app.services.watchdog.task_monitor.lock_and_set",
        side_effect=_flaky_lock_and_set(task_a.id),
    ), patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock):
        recovered = await monitor._recover_orphaned_tasks(session)  # must not raise

    await session.refresh(task_a)
    await session.refresh(task_b)

    assert task_a.status == "in_progress"
    assert task_b.status == "inbox"
    assert recovered == 1


@pytest.mark.asyncio
async def test_auto_advance_next_phase_409_no_stray_event_and_no_raise(
    session, make_board,
):
    """Direct unit test of the nested, non-loop call site (~line 646).

    Guards the reorder fix: lock_and_set() now runs BEFORE
    record_task_event() specifically so a lost race can't leave a phantom
    'auto_advance_phase' TaskEvent for a transition that never happened —
    the caller (_check_phase_completions) still runs
    _update_project_progress() afterwards in the same tick, which commits
    unconditionally and would otherwise persist that phantom event."""
    from app.models.board import Project

    board = await make_board(name="409 Board D", slug="409-board-d")
    project = Project(
        id=uuid.uuid4(), board_id=board.id, name="P", status="active", project_type="feature",
    )
    session.add(project)
    await session.commit()

    completed_parent = Task(
        id=uuid.uuid4(), board_id=board.id, project_id=project.id,
        title="Completed Phase", status="done", sort_order=1,
    )
    next_phase = Task(
        id=uuid.uuid4(), board_id=board.id, project_id=project.id,
        title="Next Phase", status="inbox", sort_order=2,
    )
    session.add_all([completed_parent, next_phase])
    await session.commit()
    await session.refresh(completed_parent)
    await session.refresh(next_phase)

    monitor = TaskMonitorMixin()
    with patch(
        "app.services.watchdog.task_monitor.lock_and_set",
        side_effect=_flaky_lock_and_set(next_phase.id),
    ):
        await monitor._auto_advance_next_phase(session, completed_parent)  # must not raise

    await session.refresh(next_phase)
    assert next_phase.status == "inbox"  # untouched, retried next tick

    events = (await session.exec(
        select(ActivityEvent).where(
            ActivityEvent.task_id == next_phase.id,
            ActivityEvent.event_type == "task.status_changed",
        )
    )).all()
    assert events == [], "lost race must not leave a phantom status_changed event"

    from app.models.task import TaskEvent
    task_events = (await session.exec(
        select(TaskEvent).where(TaskEvent.task_id == next_phase.id)
    )).all()
    assert task_events == [], "lost race must not leave a phantom TaskEvent"
