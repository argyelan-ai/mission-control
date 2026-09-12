"""Second stage: unanswered lead messages escalate to the operator — once.

Stage-1 messages (``watchdog_notify`` from the silent-card watchdog,
``blocker_lead_notify`` from blocker lead-triage / clarification FYI)
address the Board Lead on the card. If the Lead does not react within
30 minutes, the operator gets exactly one escalation (pending
``lead_escalation`` Approval + ``lead_escalated_notify`` comment).

"Lead reaction" is explicit: a Lead-authored comment on the card or a
Lead-authored status change (TaskEvent), both after the stage-1 message.

Conventions: in-memory SQLite ``test_engine``, ``make_board`` /
``make_agent`` / ``make_task``. No fleet names.
"""
from __future__ import annotations

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
               new_callable=AsyncMock) as emit, \
         patch("app.services.operator_approvals.send_approval",
               new_callable=AsyncMock) as push:
        svc = WatchdogService()
        await svc._check_lead_notify_escalations(session)
    return emit, push


async def _comments(task_id, comment_type: str | None = None):
    from app.models.task import TaskComment

    async with _session() as s:
        q = select(TaskComment).where(TaskComment.task_id == task_id)
        if comment_type is not None:
            q = q.where(TaskComment.comment_type == comment_type)
        return list((await s.exec(q)).all())


async def _approvals(task_id, action_type: str | None = None):
    from app.models.approval import Approval

    async with _session() as s:
        q = select(Approval).where(Approval.task_id == task_id)
        if action_type is not None:
            q = q.where(Approval.action_type == action_type)
        return list((await s.exec(q)).all())


async def _reload(task_id):
    from app.models.task import Task

    async with _session() as s:
        return await s.get(Task, task_id)


async def _add_stage1(task_id, comment_type: str = "watchdog_notify",
                      age_minutes: int = 45):
    from app.models.task import TaskComment

    async with _session() as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="system",
            comment_type=comment_type,
            content="STILLE KARTE: stage-1 lead message",
            created_at=utcnow() - timedelta(minutes=age_minutes),
        ))
        await s.commit()


async def _add_lead_reaction_comment(task_id, lead_id, age_minutes: int = 10):
    from app.models.task import TaskComment

    async with _session() as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="agent",
            author_agent_id=lead_id,
            comment_type="progress",
            content="Lead: on it",
            created_at=utcnow() - timedelta(minutes=age_minutes),
        ))
        await s.commit()


async def _add_lead_reaction_status(task_id, lead_id, age_minutes: int = 10):
    from app.models.task import TaskEvent

    async with _session() as s:
        s.add(TaskEvent(
            task_id=task_id,
            from_status="in_progress",
            to_status="review",
            changed_by="agent",
            agent_id=lead_id,
            created_at=utcnow() - timedelta(minutes=age_minutes),
        ))
        await s.commit()


# ── Escalation fires ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unanswered_watchdog_notify_escalates_once(
    make_board, make_agent, make_task,
):
    """watchdog_notify + 30min no lead reaction → operator escalation."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           ack_at=now - timedelta(minutes=75),
                           started_at=now - timedelta(minutes=75),
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, "watchdog_notify", age_minutes=45)

    async with _session() as s:
        _emit, push = await _run_check(s)

    escalations = await _comments(task.id, "lead_escalated_notify")
    assert len(escalations) == 1, "exactly one escalation, no stack"
    assert "ESKALATION STUFE 2" in escalations[0].content
    assert lead.name in escalations[0].content
    approvals = await _approvals(task.id, "lead_escalation")
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    push.assert_awaited_once()
    refreshed = await _reload(task.id)
    assert refreshed.status == "in_progress", "watchdog must not change status"


@pytest.mark.asyncio
async def test_unanswered_blocker_lead_notify_escalates(
    make_board, make_agent, make_task,
):
    """blocker_lead_notify hits the same dead end — same second stage."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Blocked card",
                           status="blocked",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, "blocker_lead_notify", age_minutes=45)

    async with _session() as s:
        await _run_check(s)

    escalations = await _comments(task.id, "lead_escalated_notify")
    assert len(escalations) == 1


# ── Lead reaction suppresses ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lead_comment_after_stage1_is_a_reaction(
    make_board, make_agent, make_task,
):
    """Lead commented after the stage-1 message → no escalation."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, age_minutes=45)
    await _add_lead_reaction_comment(task.id, lead.id, age_minutes=10)

    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "lead_escalated_notify") == []


@pytest.mark.asyncio
async def test_lead_status_change_after_stage1_is_a_reaction(
    make_board, make_agent, make_task,
):
    """Lead-authored status change (TaskEvent) counts as reaction."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, age_minutes=45)
    await _add_lead_reaction_status(task.id, lead.id, age_minutes=10)

    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "lead_escalated_notify") == []


@pytest.mark.asyncio
async def test_other_agent_activity_is_not_a_lead_reaction(
    make_board, make_agent, make_task,
):
    """A non-lead agent commenting does not answer the lead message."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, age_minutes=45)
    await _add_lead_reaction_comment(task.id, worker.id, age_minutes=10)

    async with _session() as s:
        await _run_check(s)

    assert len(await _comments(task.id, "lead_escalated_notify")) == 1


@pytest.mark.asyncio
async def test_lead_comment_before_stage1_is_not_a_reaction(
    make_board, make_agent, make_task,
):
    """Reaction must come AFTER the stage-1 message, not before."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=90))
    await _add_lead_reaction_comment(task.id, lead.id, age_minutes=60)
    await _add_stage1(task.id, age_minutes=45)

    async with _session() as s:
        await _run_check(s)

    assert len(await _comments(task.id, "lead_escalated_notify")) == 1


# ── Exactly once / dedup ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_tick_does_not_repeat(make_board, make_agent, make_task):
    """Same silent phase → one escalation, not a stack of them."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, age_minutes=45)

    async with _session() as s:
        await _run_check(s)
    async with _session() as s:
        await _run_check(s)

    assert len(await _comments(task.id, "lead_escalated_notify")) == 1
    assert len(await _approvals(task.id, "lead_escalation")) == 1


@pytest.mark.asyncio
async def test_new_stage1_after_reaction_reopens_window(
    make_board, make_agent, make_task,
):
    """Reaction, then a NEW stage-1 message unanswered → escalates again."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=120))
    # Phase 1: stage-1 90min ago, escalated 60min ago.
    await _add_stage1(task.id, age_minutes=90)
    async with _session() as s:
        from app.models.task import TaskComment
        s.add(TaskComment(
            task_id=task.id, author_type="system",
            comment_type="lead_escalated_notify",
            content="ESKALATION STUFE 2: previous phase",
            created_at=utcnow() - timedelta(minutes=60),
        ))
        await s.commit()
    # Reaction ended phase 1.
    await _add_lead_reaction_comment(task.id, lead.id, age_minutes=50)
    # Phase 2: new stage-1 message 45min ago, unanswered since.
    await _add_stage1(task.id, age_minutes=45)

    async with _session() as s:
        await _run_check(s)

    assert len(await _comments(task.id, "lead_escalated_notify")) == 2, \
        "new silent phase legitimately opens a new escalation"


# ── Guards ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_stage1_within_window_waits(
    make_board, make_agent, make_task,
):
    """Stage-1 message younger than 30min → not yet (30min window)."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=40))
    await _add_stage1(task.id, age_minutes=10)

    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "lead_escalated_notify") == []


@pytest.mark.asyncio
async def test_pending_approval_suppresses_second_channel(
    make_board, make_agent, make_task,
):
    """Operator already holds a pending decision → no parallel escalation."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, age_minutes=45)
    async with _session() as s:
        from app.models.approval import Approval
        s.add(Approval(
            board_id=board.id, task_id=task.id, agent_id=worker.id,
            action_type="blocker_decision",
            description="operator already on it",
            status="pending",
        ))
        await s.commit()

    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "lead_escalated_notify") == []


@pytest.mark.asyncio
async def test_terminal_task_not_escalated(make_board, make_agent, make_task):
    """A done card needs no operator escalation for an old lead message."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Finished card",
                           status="done",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, age_minutes=45)

    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "lead_escalated_notify") == []


@pytest.mark.asyncio
async def test_no_lead_no_escalation(make_board, make_agent, make_task):
    """Without a Board Lead there is nobody to have reacted — skip."""
    now = utcnow()
    board = await make_board(name="B", slug=f"le-{uuid.uuid4().hex[:8]}")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_stage1(task.id, age_minutes=45)

    async with _session() as s:
        await _run_check(s)

    assert await _comments(task.id, "lead_escalated_notify") == []


def test_escalation_wired_into_check_all():
    """Second stage must run in _check_all, before orphan recovery."""
    import inspect

    from app.services.watchdog.core import WatchdogService

    src = inspect.getsource(WatchdogService._check_all)
    assert "_check_lead_notify_escalations" in src
    assert src.index("_check_lead_notify_escalations") < src.index(
        "_recover_orphaned_tasks"
    )
