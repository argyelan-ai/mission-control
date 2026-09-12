"""Silent-card watchdog: report-only for in_progress AND waiting.

A card with no agent turn and no non-system comment for 30 minutes is
reported once to the Board Lead via ``watchdog_notify``. Status is never
changed. Dedup is the last watchdog_notify vs. last real activity (DB,
not a Redis TTL — that is what stacked identical reminders overnight).

Conventions: in-memory SQLite ``test_engine``, ``make_board`` /
``make_agent`` / ``make_task``. No fleet names.
"""
from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.utils import utcnow


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _run_check(session):
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock) as emit:
        svc = WatchdogService()
        await svc._check_silent_cards(session)
    return emit


async def _comments(task_id, comment_type: str | None = None):
    from app.models.task import TaskComment

    async with _session() as s:
        q = select(TaskComment).where(TaskComment.task_id == task_id)
        if comment_type is not None:
            q = q.where(TaskComment.comment_type == comment_type)
        return list((await s.exec(q)).all())


async def _reload(task_id):
    from app.models.task import Task

    async with _session() as s:
        return await s.get(Task, task_id)


async def _make_silent_setup(
    make_board, make_agent, make_task, *,
    status="in_progress",
    silent_minutes=45,
    with_lead=True,
    activity_age_minutes=45,
    bind_current_task=True,
    last_activity_none=False,
    run_control=None,
    review_decision=None,
    is_archived=False,
    board_kwargs=None,
):
    """Board + worker + optional lead + a card that looks silent."""
    now = utcnow()
    past = now - timedelta(minutes=silent_minutes)
    board = await make_board(
        name="Silent Board",
        slug=f"sc-{uuid.uuid4().hex[:8]}",
        is_archived=is_archived,
        **(board_kwargs or {}),
    )
    lead = None
    if with_lead:
        lead = await make_agent(
            name="Lead",
            board_id=board.id,
            is_board_lead=True,
            role="lead",
        )
    worker = await make_agent(
        name="Worker",
        board_id=board.id,
        is_board_lead=False,
        role="developer",
        last_seen_at=now,
        last_task_activity_at=(
            None if last_activity_none
            else now - timedelta(minutes=activity_age_minutes)
        ),
    )
    task_kwargs = dict(
        board_id=board.id,
        title="Silent card",
        status=status,
        assigned_agent_id=worker.id,
        ack_at=past,
        started_at=past,
        updated_at=past,
    )
    if run_control is not None:
        task_kwargs["run_control"] = run_control
    if review_decision is not None:
        task_kwargs["review_decision"] = review_decision
    task = await make_task(**task_kwargs)
    if bind_current_task:
        async with _session() as s:
            from app.models.agent import Agent
            a = await s.get(Agent, worker.id)
            a.current_task_id = task.id
            s.add(a)
            await s.commit()
    return board, lead, worker, task


# ── Wiring ──────────────────────────────────────────────────────────────


def test_silent_cards_wired_before_orphan_recovery():
    """Must run in _check_all, and before orphan recovery (which mutates)."""
    from app.services.watchdog.core import WatchdogService

    src = inspect.getsource(WatchdogService._check_all)
    assert "_check_silent_cards" in src
    assert src.index("_check_silent_cards") < src.index("_recover_orphaned_tasks")


# ── Happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_silent_in_progress_reports_once_to_lead(
    make_board, make_agent, make_task,
):
    _board, lead, _worker, task = await _make_silent_setup(
        make_board, make_agent, make_task, status="in_progress",
    )
    original_status = task.status

    async with _session() as s:
        emit = await _run_check(s)

    refreshed = await _reload(task.id)
    assert refreshed.status == original_status, "watchdog must not change status"
    notes = await _comments(task.id, "watchdog_notify")
    assert len(notes) == 1
    assert notes[0].author_type == "system"
    assert "STILLE KARTE" in notes[0].content
    assert lead.name in notes[0].content
    assert "Board-Lead" in notes[0].content
    assert "NICHT" in notes[0].content
    emit.assert_awaited()
    assert any(
        call.args[1] == "task.silent_card" for call in emit.await_args_list
    )


@pytest.mark.asyncio
async def test_second_tick_does_not_restack(
    make_board, make_agent, make_task,
):
    _board, _lead, _worker, task = await _make_silent_setup(
        make_board, make_agent, make_task,
    )
    async with _session() as s:
        await _run_check(s)
    async with _session() as s:
        await _run_check(s)

    notes = await _comments(task.id, "watchdog_notify")
    assert len(notes) == 1, "exactly one notify per silent phase, not a stack"


@pytest.mark.asyncio
async def test_silent_waiting_parent_all_children_done(
    make_board, make_agent, make_task,
):
    """The 61/61 case: waiting parent, every child done, nobody closed it."""
    now = utcnow()
    past = now - timedelta(minutes=45)
    _board, lead, worker, parent = await _make_silent_setup(
        make_board, make_agent, make_task,
        status="waiting",
        bind_current_task=False,
    )
    for i in range(2):
        child = await make_task(
            board_id=parent.board_id,
            title=f"Child {i}",
            status="done",
            parent_task_id=parent.id,
            assigned_agent_id=worker.id,
            completed_at=past,
            updated_at=past,
        )
        async with _session() as s:
            from app.models.task import Task
            c = await s.get(Task, child.id)
            c.completed_at = past
            c.updated_at = past
            s.add(c)
            await s.commit()

    async with _session() as s:
        await _run_check(s)

    refreshed = await _reload(parent.id)
    assert refreshed.status == "waiting"
    notes = await _comments(parent.id, "watchdog_notify")
    assert len(notes) == 1
    assert "2/2 done" in notes[0].content
    assert lead.name in notes[0].content


@pytest.mark.asyncio
async def test_parent_with_open_child_is_skipped(
    make_board, make_agent, make_task,
):
    _board, _lead, worker, parent = await _make_silent_setup(
        make_board, make_agent, make_task,
        status="in_progress",
        bind_current_task=False,
    )
    await make_task(
        board_id=parent.board_id,
        title="Still running child",
        status="in_progress",
        parent_task_id=parent.id,
        assigned_agent_id=worker.id,
    )
    async with _session() as s:
        await _run_check(s)

    assert await _comments(parent.id, "watchdog_notify") == []


# ── Activity / dedup ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_agent_comment_is_not_silent(
    make_board, make_agent, make_task,
):
    _board, _lead, worker, task = await _make_silent_setup(
        make_board, make_agent, make_task,
    )
    async with _session() as s:
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=worker.id,
            comment_type="progress",
            content="still working",
            created_at=utcnow() - timedelta(minutes=5),
        ))
        await s.commit()

    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "watchdog_notify") == []


@pytest.mark.asyncio
async def test_fresh_agent_turn_is_not_silent(
    make_board, make_agent, make_task,
):
    _board, _lead, worker, task = await _make_silent_setup(
        make_board, make_agent, make_task,
        activity_age_minutes=1,
    )
    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "watchdog_notify") == []


@pytest.mark.asyncio
async def test_system_comment_does_not_count_as_activity(
    make_board, make_agent, make_task,
):
    """Counting watchdog_notify as activity is what restacks reminders."""
    _board, _lead, _worker, task = await _make_silent_setup(
        make_board, make_agent, make_task,
    )
    async with _session() as s:
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task.id,
            author_type="system",
            comment_type="recovery_recap",
            content="please continue",
            created_at=utcnow() - timedelta(minutes=5),
        ))
        await s.commit()

    async with _session() as s:
        await _run_check(s)

    notes = await _comments(task.id, "watchdog_notify")
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_existing_watchdog_notify_in_phase_suppresses_ours(
    make_board, make_agent, make_task,
):
    """Another watchdog already flagged this card → one message, not two."""
    _board, _lead, _worker, task = await _make_silent_setup(
        make_board, make_agent, make_task,
    )
    async with _session() as s:
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task.id,
            author_type="system",
            comment_type="watchdog_notify",
            content="LIFECYCLE-WATCHDOG: already nudged",
            created_at=utcnow() - timedelta(minutes=10),
        ))
        await s.commit()

    async with _session() as s:
        await _run_check(s)

    notes = await _comments(task.id, "watchdog_notify")
    assert len(notes) == 1
    assert "LIFECYCLE-WATCHDOG" in notes[0].content


@pytest.mark.asyncio
async def test_new_silent_phase_after_activity_allows_second_notify(
    make_board, make_agent, make_task,
):
    """Previous notify, then real activity, then 30min silence → new phase."""
    _board, _lead, worker, task = await _make_silent_setup(
        make_board, make_agent, make_task,
        silent_minutes=90,
        activity_age_minutes=90,
    )
    async with _session() as s:
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task.id,
            author_type="system",
            comment_type="watchdog_notify",
            content="STILLE KARTE: previous phase",
            created_at=utcnow() - timedelta(minutes=80),
        ))
        s.add(TaskComment(
            task_id=task.id,
            author_type="agent",
            author_agent_id=worker.id,
            comment_type="progress",
            content="back to work",
            created_at=utcnow() - timedelta(minutes=45),
        ))
        await s.commit()

    async with _session() as s:
        await _run_check(s)

    notes = await _comments(task.id, "watchdog_notify")
    assert len(notes) == 2, "new silent phase after real activity must notify again"


# ── Guards ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archived_board_skipped(make_board, make_agent, make_task):
    _board, _lead, _worker, task = await _make_silent_setup(
        make_board, make_agent, make_task, is_archived=True,
    )
    async with _session() as s:
        await _run_check(s)
    assert await _comments(task.id, "watchdog_notify") == []


@pytest.mark.asyncio
async def test_no_lead_skips_without_operator_escalate(
    make_board, make_agent, make_task,
):
    _board, lead, _worker, task = await _make_silent_setup(
        make_board, make_agent, make_task, with_lead=False,
    )
    assert lead is None
    async with _session() as s:
        await _run_check(s)
    assert await _comments(task.id, "watchdog_notify") == []
    refreshed = await _reload(task.id)
    assert refreshed.status == "in_progress"


@pytest.mark.asyncio
async def test_hold_and_stopped_skipped(make_board, make_agent, make_task):
    _b1, _, _, held = await _make_silent_setup(
        make_board, make_agent, make_task, review_decision="hold",
    )
    _b2, _, _, stopped = await _make_silent_setup(
        make_board, make_agent, make_task, run_control="stopped",
    )
    async with _session() as s:
        await _run_check(s)
    assert await _comments(held.id, "watchdog_notify") == []
    assert await _comments(stopped.id, "watchdog_notify") == []


@pytest.mark.asyncio
async def test_pending_approval_skips(make_board, make_agent, make_task):
    board, _lead, worker, task = await _make_silent_setup(
        make_board, make_agent, make_task,
    )
    async with _session() as s:
        from app.models.approval import Approval
        s.add(Approval(
            board_id=board.id,
            task_id=task.id,
            agent_id=worker.id,
            action_type="blocker_decision",
            description="already with operator",
            status="pending",
        ))
        await s.commit()

    async with _session() as s:
        await _run_check(s)
    assert await _comments(task.id, "watchdog_notify") == []


@pytest.mark.asyncio
async def test_never_creates_approval_or_flips_status(
    make_board, make_agent, make_task,
):
    _board, _lead, _worker, task = await _make_silent_setup(
        make_board, make_agent, make_task, status="waiting",
        bind_current_task=False,
    )
    async with _session() as s:
        await _run_check(s)

    refreshed = await _reload(task.id)
    assert refreshed.status == "waiting"
    async with _session() as s:
        from app.models.approval import Approval
        approvals = list((await s.exec(
            select(Approval).where(Approval.task_id == task.id)
        )).all())
    assert approvals == []
    notes = await _comments(task.id, "watchdog_notify")
    assert len(notes) == 1
