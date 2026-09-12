"""Operator override ("Selbst entscheiden") stops the reviewer's RUNNING turn.

execute_review_decision (POST /tasks/{id}/review) is the same endpoint for
agent reviewers and the operator. When the OPERATOR decides (actor_user_id,
actor_agent=None) while a reviewer agent holds the card, the reviewer's live
turn used to keep running and die at its end with a bare 409 ("Task ist
nicht im Review") — work lost without explanation.

Now: operator override writes a system comment naming decision + decider and
sets run_control="stopped" — the heartbeat control channel
(routers/agents.py:_heartbeat_control) converts that into a HARD interrupt
for the bridge's live turn. Guardrails:
  - agent-made decisions (actor_agent set) never stop anything — the
    "reviewer" is the caller itself;
  - a card with NO reviewer agent assigned is never touched (operator-only
    cards stay stop-free);
  - the stop flag PERSISTS past the decision request: the reviewer's next
    heartbeat/poll must still see it to receive the hard interrupt (the
    same-request clear was review blocker B1); downstream requeue/park/
    resume paths reset it when the card moves on.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task, TaskComment
from tests.conftest import test_engine


async def _decide(task_row, board_id, decision, comment, *, actor_agent=None):
    """Run execute_review_decision against a fresh row with event mocks."""
    from app.services.task_lifecycle import execute_review_decision

    with (
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.record_task_event", new_callable=AsyncMock),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, task_row.id)
            await execute_review_decision(
                s, t, board_id, decision, comment,
                actor_agent=actor_agent,
            )


@pytest.mark.asyncio
async def test_operator_override_stops_running_reviewer_turn(
    make_board, make_agent, make_task,
):
    """Operator decision while a reviewer holds the card: system comment +
    run_control=stopped so the heartbeat channel hard-interrupts the turn."""
    board = await make_board(name="Override Board", slug="override-board")
    reviewer = await make_agent(name="Reviewer-Override", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Override Stop",
        status="review", assigned_agent_id=reviewer.id,
    )
    await _decide(task, board.id, "approve", "Operator entscheidet selbst.")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        # Card moved out of review, but the one-turn stop flag PERSISTS:
        # the reviewer's next heartbeat must still deliver the hard
        # interrupt (same-request clear was review blocker B1).
        assert t.status == "done"
        assert t.run_control == "stopped"
        from app.routers.agents import _heartbeat_control
        control = _heartbeat_control(t, reviewer.id, [], None)
        assert control == {
            "interrupt": "hard",
            "reason": "run_control=stopped (Stop durch Operator)",
        }
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
            .order_by(TaskComment.created_at.asc())
        )).all()
        stop_notes = [c for c in comments if c.comment_type == "system"
                      and "Operator-Override" in c.content]
        assert len(stop_notes) == 1
        assert "approve" in stop_notes[0].content
        assert "Operator" in stop_notes[0].content


@pytest.mark.asyncio
async def test_agent_decision_does_not_stop_or_comment(make_board, make_agent, make_task):
    """The reviewer deciding itself (agent path) must NOT stop the turn nor
    write the override comment — that decision IS the reviewer's own turn."""
    board = await make_board(name="Agent Path Board", slug="agent-path-board")
    reviewer = await make_agent(name="Reviewer-Self", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Self Decide",
        status="review", assigned_agent_id=reviewer.id,
    )

    await _decide(task, board.id, "approve", "LGTM", actor_agent=reviewer)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "done"
        # Agent path never sets the flag — nothing to persist.
        assert t.run_control is None
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert not [c for c in comments if "Operator-Override" in c.content]


@pytest.mark.asyncio
async def test_operator_override_with_no_reviewer_is_noop(make_board, make_agent, make_task):
    """Guardrail: a card the operator holds alone (no assigned reviewer) is
    never stopped — nothing to interrupt, no stop comment, no flag."""
    board = await make_board(name="Solo Board", slug="solo-board")
    task = await make_task(
        board_id=board.id, title="Solo Review",
        status="review", assigned_agent_id=None,
    )

    await _decide(task, board.id, "approve", "Operator entscheidet allein.")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "done"
        assert t.run_control is None
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        assert not [c for c in comments if "Operator-Override" in c.content]


@pytest.mark.asyncio
async def test_operator_override_on_request_changes_keeps_flow(
    make_board, make_agent, make_task,
):
    """request_changes by the operator: stop comment written, reviewer
    released, and the stop flag PERSISTS with the final inbox state so the
    reviewer's next heartbeat still hard-interrupts the live turn."""
    board = await make_board(name="Rework Board", slug="rework-board")
    reviewer = await make_agent(name="Reviewer-Rework", board_id=board.id, role="reviewer")
    developer = await make_agent(name="Dev-Rework", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Rework Probe",
        status="review", assigned_agent_id=reviewer.id,
    )
    # Developer provenance for handle_review_rejection: the rework comment.
    task.dispatch_intent = "review_rework"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.dispatch_intent = "review_rework"
        s.add(t)
        await s.commit()

    with (
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.record_task_event", new_callable=AsyncMock),
        patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock),
    ):
        from app.services.task_lifecycle import execute_review_decision
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, task.id)
            await execute_review_decision(
                s, t, board.id, "request_changes",
                "not ship-ready — bitte Tests ergaenzen.",
                actor_agent=None,
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        # Flag persists with the final state (inbox) — the reviewer's next
        # heartbeat must still see it (B1: same-request clear erased the
        # interrupt before it was ever delivered).
        assert t.status == "inbox"
        assert t.run_control == "stopped"
        from app.routers.agents import _heartbeat_control
        control = _heartbeat_control(t, reviewer.id, [], None)
        assert control is not None and control["interrupt"] == "hard"
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
            .order_by(TaskComment.created_at.asc())
        )).all()
        stop_notes = [c for c in comments if c.comment_type == "system"
                      and "Operator-Override" in c.content]
        assert len(stop_notes) == 1
        assert "request_changes" in stop_notes[0].content
