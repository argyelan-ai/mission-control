"""Receiver for the task.stuck escalation event (hole 3, 2026-09-18).

``task.stuck`` was written by the stale-check circuit breaker but nothing
consumed it — UI badge mapping and an agent pull endpoint only. The relay
(services/stuck_escalation.py, routed from activity.emit_event) turns the
event into the lead-visible alarm channel that worked that night: a
``watchdog_notify`` comment on the card.

Conventions: in-memory SQLite ``test_engine``, real ``emit_event`` (the
fakeredis singleton fixture absorbs the SSE broadcast; the Discord leg
no-ops without webhook config).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlmodel import select

from app.utils import utcnow


@asynccontextmanager
async def _session():
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _emit_stuck(task, agent, **detail_overrides):
    """Emit a REAL task.stuck through activity.emit_event — the production
    path (task_runner._check_stale_in_progress → emit_event)."""
    from app.services.activity import emit_event

    detail = {
        "agent_name": agent.name,
        "task_title": task.title,
        "minutes_since_activity": 95,
        "check_count": 4,
        **detail_overrides,
    }
    async with _session() as s:
        t = await s.get(type(task), task.id)
        await emit_event(
            s, "task.stuck",
            f"'{task.title}' bei {agent.name}: seit 95min stuck",
            severity="error",
            board_id=t.board_id,
            task_id=t.id,
            agent_id=agent.id,
            detail=detail,
        )


async def _notify_comments(task_id):
    from app.models.task import TaskComment

    async with _session() as s:
        q = select(TaskComment).where(
            TaskComment.task_id == task_id,
            TaskComment.comment_type == "watchdog_notify",
        )
        return list((await s.exec(q)).all())


async def _make_setup(make_board, make_agent, make_task, *, with_lead=True):
    board = await make_board(name="Stuck Board", slug=f"se-{uuid.uuid4().hex[:8]}")
    lead = None
    if with_lead:
        lead = await make_agent(
            name="Lead", board_id=board.id, is_board_lead=True, role="lead",
        )
    worker = await make_agent(
        name="Worker", board_id=board.id, is_board_lead=False, role="developer",
    )
    task = await make_task(
        board_id=board.id,
        title="Stuck escalation task",
        status="in_progress",
        assigned_agent_id=worker.id,
        ack_at=utcnow() - timedelta(hours=2),
        started_at=utcnow() - timedelta(hours=2),
    )
    return board, lead, worker, task


@pytest.mark.asyncio
async def test_stuck_event_relayed_as_watchdog_notify_to_lead(
    make_board, make_agent, make_task,
):
    """The receiver: a task.stuck event MUST land as a lead-visible comment
    on the card — not stay a log entry."""
    _board, lead, worker, task = await _make_setup(make_board, make_agent, make_task)

    await _emit_stuck(task, worker)

    notes = await _notify_comments(task.id)
    assert len(notes) == 1
    assert lead.name in notes[0].content
    assert task.title in notes[0].content


@pytest.mark.asyncio
async def test_stuck_relay_deduped_against_fresh_watchdog_notify(
    make_board, make_agent, make_task,
):
    """A watchdog that just spoke on the card must not be double-alarmed."""
    from app.models.task import TaskComment

    _board, _lead, worker, task = await _make_setup(make_board, make_agent, make_task)
    async with _session() as s:
        s.add(TaskComment(
            task_id=task.id,
            author_type="system",
            comment_type="watchdog_notify",
            content="LIFECYCLE-WATCHDOG: nudge 5 minutes ago",
            created_at=utcnow() - timedelta(minutes=5),
        ))
        await s.commit()

    await _emit_stuck(task, worker)

    notes = await _notify_comments(task.id)
    assert len(notes) == 1, "fresh watchdog_notify must suppress the relay"
    assert "LIFECYCLE-WATCHDOG" in notes[0].content


@pytest.mark.asyncio
async def test_stuck_relay_without_lead_still_alarms_operator(
    make_board, make_agent, make_task,
):
    """No board lead → the alarm must still land (operator-addressed)."""
    _board, _lead, worker, task = await _make_setup(
        make_board, make_agent, make_task, with_lead=False,
    )

    await _emit_stuck(task, worker)

    notes = await _notify_comments(task.id)
    assert len(notes) == 1
    assert "Operator" in notes[0].content


@pytest.mark.asyncio
async def test_non_stuck_events_are_not_relayed(
    make_board, make_agent, make_task,
):
    """The receiver is scoped to task.stuck — other events stay untouched."""
    from app.services.activity import emit_event

    _board, _lead, worker, task = await _make_setup(make_board, make_agent, make_task)
    async with _session() as s:
        await emit_event(
            s, "task.some_other_event", "unrelated",
            severity="error", board_id=_board.id, task_id=task.id,
            agent_id=worker.id,
        )

    assert await _notify_comments(task.id) == []
