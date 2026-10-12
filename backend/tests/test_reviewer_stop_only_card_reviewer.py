"""Operator override stops a reviewer turn only for an agent that REVIEWED this card.

Live incident (11.10.2026, after PR #780): the board's reviewer-role agent
built a card itself and held it in review. The operator requested changes.
stop_running_reviewer_turn keyed purely on ``role == "reviewer"`` and set
``run_control="stopped"`` on the builder's card. handle_review_rejection then
routed the rework back to that same builder, check_dispatch_allowed vetoed
the re-dispatch on ``run_control: stopped`` and parked the card in inbox
with the flag still set. The builder's poll answered ``state: stopped`` on
every cycle, its bridge cleared the dispatch cache, and the watchdog
re-dispatched forever — the card never moved again.

Rules pinned here:
  - only an agent that reviewed this card (got its ``task.review_handoff``
    or left a ``review`` comment) counts as a mid-turn reviewer — the same
    rule find_last_developer uses since #780 (shared helper);
  - a genuine reviewer mid-turn is still stopped (approve keeps the flag
    for its heartbeat);
  - when the rework is handed to a DIFFERENT agent than the stopped
    reviewer, the one-turn stop flag is cleared so the builder's re-dispatch
    is not blocked (the reviewer keeps losing the card through the
    withdrawn-task guard instead).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _add_events(board_id, task_id, *events):
    from app.models.activity import ActivityEvent

    for event_type, agent_id, detail in events:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(ActivityEvent(
                event_type=event_type, title=event_type,
                board_id=board_id, task_id=task_id, agent_id=agent_id,
                detail=detail,
            ))
            await s.commit()


async def _operator_decides(task_id, board_id, decision, comment):
    from app.models.task import Task
    from app.services.task_lifecycle import execute_review_decision

    dispatch = AsyncMock()
    with (
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.record_task_event", new_callable=AsyncMock),
        patch("app.services.dispatch.auto_dispatch_task", dispatch),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, task_id)
            await execute_review_decision(
                s, t, board_id, decision, comment,
                actor_user_id=uuid.uuid4(),
            )
    return dispatch


async def _override_notes(task_id):
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
    return [c for c in comments if "Operator-Override" in (c.content or "")]


_SUBMIT = {"old_status": "in_progress", "new_status": "review"}


@pytest.mark.asyncio
async def test_builder_with_reviewer_role_is_not_stopped_on_request_changes(
    fake_redis, make_board, make_agent, make_task,
):
    """Live regression: the reviewer-role agent built and holds its own card,
    no review handoff for it. Operator request_changes must NOT stop it —
    the rework goes back to it and is actually dispatched."""
    from app.models.task import Task

    board = await make_board(name="Builder Reviewer", slug=f"br-{uuid.uuid4().hex[:8]}")
    await make_agent(name="Lead", role="lead", board_id=board.id, is_board_lead=True)
    builder = await make_agent(
        name="ReviewerRoleBuilder", role="reviewer", board_id=board.id,
        agent_runtime="host",
    )
    task = await make_task(
        board_id=board.id, title="Built by the reviewer-role agent",
        status="review", assigned_agent_id=builder.id,
    )
    await _add_events(board.id, task.id, ("task.status_changed", builder.id, _SUBMIT))

    dispatch = await _operator_decides(
        task.id, board.id, "request_changes", "not ship-ready — one more change",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.run_control is None, "builder's card was stop-flagged"
        assert t.status == "inbox"
        assert t.assigned_agent_id == builder.id
    assert await _override_notes(task.id) == []
    dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_genuine_reviewer_with_handoff_is_still_stopped_on_approve(
    fake_redis, make_board, make_agent, make_task,
):
    """A reviewer that received this card's review handoff and still holds
    it is mid-turn: the operator override stops it and the flag persists
    for its heartbeat (approve leaves the card assigned to it)."""
    from app.models.task import Task

    board = await make_board(name="Genuine Approve", slug=f"ga-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(name="Dev", role="developer", board_id=board.id)
    reviewer = await make_agent(name="Rev", role="reviewer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Genuine review",
        status="review", assigned_agent_id=reviewer.id,
    )
    await _add_events(
        board.id, task.id,
        ("task.status_changed", dev.id, _SUBMIT),
        ("task.review_handoff", reviewer.id, None),
    )

    await _operator_decides(task.id, board.id, "approve", "Operator entscheidet selbst.")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "done"
        assert t.run_control == "stopped"
    assert len(await _override_notes(task.id)) == 1


@pytest.mark.asyncio
async def test_genuine_reviewer_stopped_rework_to_builder_is_dispatchable(
    fake_redis, make_board, make_agent, make_task,
):
    """Defence in depth: the genuine reviewer is stopped (comment written),
    but the rework goes to a DIFFERENT agent — the builder. The one-turn
    stop flag must not follow the card to the builder, otherwise the
    re-dispatch is vetoed and the card hangs in inbox forever."""
    from app.models.task import Task

    board = await make_board(name="Genuine Rework", slug=f"gr-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(
        name="Dev", role="developer", board_id=board.id, agent_runtime="host",
    )
    reviewer = await make_agent(name="Rev", role="reviewer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Genuine review, rework",
        status="review", assigned_agent_id=reviewer.id,
    )
    await _add_events(
        board.id, task.id,
        ("task.status_changed", dev.id, _SUBMIT),
        ("task.review_handoff", reviewer.id, None),
    )

    dispatch = await _operator_decides(
        task.id, board.id, "request_changes", "not ship-ready — add tests",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "inbox"
        assert t.assigned_agent_id == dev.id
        assert t.run_control is None, "stop flag followed the card to the builder"
    notes = await _override_notes(task.id)
    assert len(notes) == 1 and "request_changes" in notes[0].content
    dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_reviewer_with_review_comment_counts_as_card_reviewer(
    fake_redis, make_board, make_agent, make_task,
):
    """The shared helper also counts a `review` comment as having reviewed
    this card (no handoff event needed)."""
    from app.models.task import TaskComment
    from app.services.work_context import agents_who_reviewed_card

    board = await make_board(name="Helper", slug=f"hp-{uuid.uuid4().hex[:8]}")
    a = await make_agent(name="A", role="reviewer", board_id=board.id)
    b = await make_agent(name="B", role="reviewer", board_id=board.id)
    c = await make_agent(name="C", role="reviewer", board_id=board.id)
    task = await make_task(board_id=board.id, title="h", status="review")
    await _add_events(board.id, task.id, ("task.review_handoff", a.id, None))
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=b.id,
            comment_type="review", content="needs work",
        ))
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=c.id,
            comment_type="progress", content="built it",
        ))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        reviewed = await agents_who_reviewed_card(s, task.id)

    assert reviewed == {a.id, b.id}
