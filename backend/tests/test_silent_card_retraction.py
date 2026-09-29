"""Retract path for silent-card alerts (candidate A, task 1aae781f).

Stage 1 (``_check_silent_cards``) posts a ``watchdog_notify`` /
``blocker_lead_notify`` comment to the Board Lead. Stage 2
(``_check_lead_notify_escalations``) escalates an unanswered stage-1
message to the operator via a pending ``lead_escalation`` Approval.
Neither stage ever retracts itself — this is what
``_check_silent_card_retractions`` adds.

"Moves again" is evidence-based, not a status flip: the same signal
(``_silent_card_last_activity_at`` — non-system comment, an agent turn
recorded on THIS card, ack/start, or a child completing) that flags a
card silent also has to show a timestamp AFTER the alert for the
retraction to fire. A bare status PATCH with no comment and no agent
turn must NOT retract anything.

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


async def _run_retract_check(session):
    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock) as emit, \
         patch("app.services.operator_approvals.update_resolved",
               new_callable=AsyncMock) as resolve_push:
        svc = WatchdogService()
        await svc._check_silent_card_retractions(session)
    return emit, resolve_push


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


async def _add_alert(task_id, comment_type: str, age_minutes: int = 0,
                     at=None):
    from app.models.task import TaskComment

    async with _session() as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="system",
            comment_type=comment_type,
            content=f"ALERT ({comment_type})",
            created_at=at if at is not None else utcnow() - timedelta(minutes=age_minutes),
        ))
        await s.commit()


async def _add_real_comment(task_id, author_agent_id, age_minutes: int = 0,
                            at=None):
    from app.models.task import TaskComment

    async with _session() as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="agent",
            author_agent_id=author_agent_id,
            comment_type="progress",
            content="back at it",
            created_at=at if at is not None else utcnow() - timedelta(minutes=age_minutes),
        ))
        await s.commit()


async def _add_pending_escalation(task_id, board_id, agent_id, age_minutes: int = 45):
    from app.models.approval import Approval

    async with _session() as s:
        approval = Approval(
            board_id=board_id,
            task_id=task_id,
            agent_id=agent_id,
            action_type="lead_escalation",
            description="Lead did not react",
            status="pending",
            created_at=utcnow() - timedelta(minutes=age_minutes),
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)
        return approval.id


# ── Stage-1 retraction (no approval involved) ───────────────────────────


@pytest.mark.asyncio
async def test_stage1_alert_retracts_on_real_activity(
    make_board, make_agent, make_task,
):
    """watchdog_notify + a real comment AFTER it → visible retraction."""
    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_alert(task.id, "watchdog_notify", age_minutes=45)
    await _add_real_comment(task.id, worker.id, age_minutes=10)

    async with _session() as s:
        emit, resolve_push = await _run_retract_check(s)

    retractions = await _comments(task.id, "watchdog_retract")
    assert len(retractions) == 1, "exactly one retraction note"
    assert "ALARM ZURUECKGEZOGEN" in retractions[0].content
    emit.assert_awaited()
    resolve_push.assert_not_awaited(), "no approval was open — nothing to push"
    refreshed = await _reload(task.id)
    assert refreshed.status == "in_progress", "retraction must not change status"


@pytest.mark.asyncio
async def test_stage1_alert_stays_open_without_real_activity(
    make_board, make_agent, make_task,
):
    """No real activity after the alert → no retraction. Still silent."""
    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_alert(task.id, "watchdog_notify", age_minutes=45)

    async with _session() as s:
        await _run_retract_check(s)

    assert await _comments(task.id, "watchdog_retract") == []


@pytest.mark.asyncio
async def test_status_flip_alone_does_not_retract(
    make_board, make_agent, make_task,
):
    """A bare status PATCH (no comment, no turn) must NOT retract the alert.

    This is the exact distinction the task brief calls out: "a status flip
    is not movement — a card can be flipped by a watchdog, a reassign, or
    a human without any work happening." updated_at moving alone must not
    count as activity (_silent_card_last_activity_at deliberately omits it).
    """
    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_alert(task.id, "watchdog_notify", age_minutes=45)

    # Simulate "a watchdog/reassign flipped the status" — updated_at moves,
    # but no comment, no ack/start change, no agent turn.
    async with _session() as s:
        from app.models.task import Task
        t = await s.get(Task, task.id)
        t.updated_at = now - timedelta(minutes=1)
        s.add(t)
        await s.commit()

    async with _session() as s:
        await _run_retract_check(s)

    assert await _comments(task.id, "watchdog_retract") == [], \
        "updated_at alone must never retract the alert"


# ── Stage-2 retraction (closes the pending Approval too) ────────────────


@pytest.mark.asyncio
async def test_stage2_escalation_retracts_and_closes_approval(
    make_board, make_agent, make_task,
):
    """lead_escalated_notify + Lead reacting → retraction AND Approval closed."""
    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    lead = await make_agent(name="Lead", board_id=board.id,
                            is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Escalated card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=120))
    await _add_alert(task.id, "lead_escalated_notify", age_minutes=20)
    approval_id = await _add_pending_escalation(
        task.id, board.id, worker.id, age_minutes=20,
    )
    await _add_real_comment(task.id, lead.id, age_minutes=5)

    async with _session() as s:
        _emit, resolve_push = await _run_retract_check(s)

    retractions = await _comments(task.id, "watchdog_retract")
    assert len(retractions) == 1

    approvals = await _approvals(task.id, "lead_escalation")
    assert len(approvals) == 1
    assert approvals[0].id == approval_id
    assert approvals[0].status == "superseded", \
        "retract must close the approval, never approve/reject it"
    assert approvals[0].resolved_at is not None
    assert approvals[0].resolver_note is not None

    resolve_push.assert_awaited_once()
    awaited_args = resolve_push.await_args.args
    assert awaited_args[0] == approval_id
    assert awaited_args[1] == "superseded"

    refreshed = await _reload(task.id)
    assert refreshed.status == "in_progress", "retraction must not change status"


@pytest.mark.asyncio
async def test_stage2_approval_stays_pending_without_activity(
    make_board, make_agent, make_task,
):
    """No activity after the stage-2 alert → Approval stays open, no retract."""
    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Escalated card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=120))
    await _add_alert(task.id, "lead_escalated_notify", age_minutes=20)
    approval_id = await _add_pending_escalation(
        task.id, board.id, worker.id, age_minutes=20,
    )

    async with _session() as s:
        await _run_retract_check(s)

    assert await _comments(task.id, "watchdog_retract") == []
    approvals = await _approvals(task.id, "lead_escalation")
    assert approvals[0].id == approval_id
    assert approvals[0].status == "pending"


# ── Dedup: one retraction per alert phase ────────────────────────────────


@pytest.mark.asyncio
async def test_retraction_is_not_reposted_for_the_same_phase(
    make_board, make_agent, make_task,
):
    """Running the check twice after one resumption posts exactly one note."""
    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=75))
    await _add_alert(task.id, "watchdog_notify", age_minutes=45)
    await _add_real_comment(task.id, worker.id, age_minutes=10)

    async with _session() as s:
        await _run_retract_check(s)
    async with _session() as s:
        await _run_retract_check(s)

    assert len(await _comments(task.id, "watchdog_retract")) == 1, \
        "second run must not repost the retraction"


@pytest.mark.asyncio
async def test_new_silence_phase_after_retraction_can_alert_again(
    make_board, make_agent, make_task,
):
    """Retract, then go silent again with a NEW alert → retract path re-arms.

    Guards against a retraction marker permanently suppressing future
    alerts on the same card (it must only dedup within one phase).
    """
    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Silent card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=200))

    # First silent phase: alert, then activity resumes, retract fires.
    await _add_alert(task.id, "watchdog_notify", age_minutes=180)
    await _add_real_comment(task.id, worker.id, age_minutes=150)
    async with _session() as s:
        await _run_retract_check(s)
    assert len(await _comments(task.id, "watchdog_retract")) == 1
    after_first_retract = utcnow()

    # Second silent phase: a NEW alert, chronologically AFTER the first
    # retraction, with no activity after it yet — must stay open (not
    # retracted a second time from stale first-phase evidence).
    await _add_alert(task.id, "watchdog_notify",
                     at=after_first_retract + timedelta(seconds=1))
    async with _session() as s:
        await _run_retract_check(s)
    assert len(await _comments(task.id, "watchdog_retract")) == 1, \
        "no activity after the second alert yet — must not retract again"

    # Now real activity lands after the second alert → retracts again.
    await _add_real_comment(task.id, worker.id,
                            at=after_first_retract + timedelta(seconds=2))
    async with _session() as s:
        await _run_retract_check(s)
    assert len(await _comments(task.id, "watchdog_retract")) == 2, \
        "second silent phase resolved → second retraction note"


# ── The point of this card: sabotage the retract path itself ────────────


@pytest.mark.asyncio
async def test_sabotage_no_retract_path_leaves_approval_stale(
    make_board, make_agent, make_task,
):
    """If the retract path is removed, the Approval never closes.

    This test calls the same evidence (real activity after the alert) but
    asserts against a hand-rolled "old" cleanup path
    (``approval_cleanup.reconcile_stale_approvals``) that is status-based,
    not evidence-based, to prove that generic mechanism does NOT cover
    ``lead_escalation`` on its own — the dedicated retract path in this
    diff is load-bearing, not redundant with existing cleanup.
    """
    from app.services.approval_cleanup import reconcile_stale_approvals

    now = utcnow()
    board = await make_board(name="B", slug=f"rt-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", board_id=board.id,
                     is_board_lead=True, role="lead")
    worker = await make_agent(name="Worker", board_id=board.id,
                              is_board_lead=False, role="developer",
                              last_seen_at=now)
    task = await make_task(board_id=board.id, title="Escalated card",
                           status="in_progress",
                           assigned_agent_id=worker.id,
                           updated_at=now - timedelta(minutes=120))
    await _add_alert(task.id, "lead_escalated_notify", age_minutes=20)
    approval_id = await _add_pending_escalation(
        task.id, board.id, worker.id, age_minutes=20,
    )
    await _add_real_comment(task.id, worker.id, age_minutes=5)

    async with _session() as s:
        await reconcile_stale_approvals(s)

    approvals = await _approvals(task.id, "lead_escalation")
    assert approvals[0].id == approval_id
    assert approvals[0].status == "pending", (
        "the generic status-based reconciliation does not know "
        "lead_escalation — without the dedicated retract path this "
        "approval would stay pending forever"
    )
