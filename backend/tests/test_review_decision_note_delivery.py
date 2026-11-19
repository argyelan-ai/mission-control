"""Approve note must NOT reach the agent; every other decision note still does.

Live bug (24.10.2026, operator report): the operator approved a card in
review ("Annehmen" + the required reason "Looks good"). The card went to
`done` — and the assigned agent still got a notification. Cause:
execute_review_decision writes the reason as an operator comment
(author_type="user", comment_type="review"), and the poll delivers EVERY
operator comment — on purpose also on `done` cards (2026-05-18 rule: a free
operator comment on a done card must still reach the agent).

Contract pinned here (all through the real /agent/me/poll delivery path,
``routers.agents._collect_and_ack_new_comments``):
  - approve note (normal decision + late "Nachtrag" note) → NOT delivered;
  - the reviewer-stop note an operator approve writes → NOT delivered either
    (same moment, same "card is done" message);
  - request_changes note → still delivered (rework) and still in the rework
    dispatch message;
  - hold note → still delivered (the card holder must learn it has to wait);
  - a free operator comment on a done card → still delivered (2026-05-18).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


async def _poll(agent_id: uuid.UUID) -> list[dict]:
    """One real poll of the comment-delivery path for this agent."""
    from app.routers.agents import _collect_and_ack_new_comments

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        out = await _collect_and_ack_new_comments(agent, s)
        await s.commit()
        return out


async def _decide(task_id, board_id, decision, comment, **kw):
    from app.services.task_lifecycle import execute_review_decision

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode",
              new_callable=AsyncMock, return_value="active"),
        patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, task_id)
            await execute_review_decision(s, t, board_id, decision, comment, **kw)


async def _late_note(task_id, board_id, decision, comment):
    from app.services.task_lifecycle import record_late_review_note

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(Task, task_id)
            await record_late_review_note(s, t, board_id, decision, comment)


async def _builder_card(make_board, make_agent, make_task, *, status="review"):
    """A card in review still held by its builder (the live shape: no
    reviewer agent on the board, the operator decides)."""
    board = await make_board(name="Note Board", slug=f"note-{uuid.uuid4().hex[:8]}")
    builder = await make_agent(name="Builder", role="developer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Small page", status=status,
        assigned_agent_id=builder.id,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=builder.id,
            comment_type="progress", content="Implementation done",
        ))
        await s.commit()
    # Prime the cursor: everything before the decision counts as seen.
    await _poll(builder.id)
    return board, builder, task


@pytest.mark.asyncio
async def test_operator_approve_delivers_nothing_to_the_agent(
    fake_redis, make_board, make_agent, make_task,
):
    board, builder, task = await _builder_card(make_board, make_agent, make_task)

    await _decide(task.id, board.id, "approve", "Looks good",
                  actor_user_id=uuid.uuid4())

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "done"
        assert t.assigned_agent_id == builder.id  # still polls this card
        notes = (await s.exec(select(TaskComment).where(
            TaskComment.task_id == task.id, TaskComment.author_type == "user",
        ))).all()
        assert [n.content for n in notes] == ["Looks good"]  # note is kept

    assert await _poll(builder.id) == []


@pytest.mark.asyncio
async def test_operator_approve_over_running_reviewer_delivers_nothing(
    fake_redis, make_board, make_agent, make_task,
):
    """Reviewer mid-turn on the card: the operator approve still hard-stops
    the turn (run_control), but neither the decision note nor the
    'Operator-Override' stop note lands in the reviewer's session."""
    from app.models.activity import ActivityEvent

    board = await make_board(name="Rev Board", slug=f"rev-{uuid.uuid4().hex[:8]}")
    reviewer = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    task = await make_task(board_id=board.id, title="Reviewed card",
                           status="review", assigned_agent_id=reviewer.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(event_type="task.review_handoff", title="handoff",
                            board_id=board.id, task_id=task.id, agent_id=reviewer.id))
        await s.commit()
    await _poll(reviewer.id)

    await _decide(task.id, board.id, "approve", "Looks good",
                  actor_user_id=uuid.uuid4())

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "done"
        assert t.run_control == "stopped"  # hard interrupt still happens
        comments = (await s.exec(select(TaskComment).where(
            TaskComment.task_id == task.id))).all()
        assert any("Operator-Override" in c.content for c in comments)

    assert await _poll(reviewer.id) == []


@pytest.mark.asyncio
async def test_late_approve_note_on_done_card_delivers_nothing(
    fake_redis, make_board, make_agent, make_task,
):
    board, builder, task = await _builder_card(
        make_board, make_agent, make_task, status="done")

    await _late_note(task.id, board.id, "approve", "Checked it later, fine.")

    delivered = await _poll(builder.id)
    assert delivered == []


@pytest.mark.asyncio
async def test_late_request_changes_note_is_still_delivered(
    fake_redis, make_board, make_agent, make_task,
):
    """Only the approve variant goes quiet — a late 'changes requested' note
    keeps reaching the agent exactly as before."""
    board, builder, task = await _builder_card(
        make_board, make_agent, make_task, status="done")

    await _late_note(task.id, board.id, "request_changes", "Button text is wrong.")

    delivered = await _poll(builder.id)
    assert len(delivered) == 1
    assert "Button text is wrong." in delivered[0]["content"]
    assert delivered[0]["source"] == "user"


@pytest.mark.asyncio
async def test_request_changes_note_still_reaches_the_builder(
    fake_redis, make_board, make_agent, make_task,
):
    """Rework: the builder gets the operator's reason via the poll AND the
    rework dispatch message (task_context_builder → dispatch_message_builder)."""
    board, builder, task = await _builder_card(make_board, make_agent, make_task)

    await _decide(task.id, board.id, "request_changes", "Fix the header spacing.",
                  actor_user_id=uuid.uuid4())

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "inbox"
        assert t.assigned_agent_id == builder.id
        assert t.dispatch_intent == "review_rework"

    delivered = await _poll(builder.id)
    review_notes = [d for d in delivered if d["comment_type"] == "review"]
    assert len(review_notes) == 1
    assert review_notes[0]["content"] == "Fix the header spacing."
    assert review_notes[0]["source"] == "user"

    from app.services.dispatch_message_builder import _build_dispatch_message
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        agent = await s.get(Agent, builder.id)
        msg = await _build_dispatch_message(t, agent, s)
    assert "Review-Feedback (ZUERST lesen)" in msg
    assert "Fix the header spacing." in msg


@pytest.mark.asyncio
async def test_hold_note_still_reaches_the_card_holder(
    fake_redis, make_board, make_agent, make_task,
):
    """Hold keeps the card in review with its holder — the note is the one
    signal telling it to wait, so it stays deliverable."""
    board, builder, task = await _builder_card(make_board, make_agent, make_task)

    await _decide(task.id, board.id, "hold", "Wait for the design decision.",
                  actor_user_id=uuid.uuid4())

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.status == "review"
        assert t.review_decision == "hold"

    delivered = await _poll(builder.id)
    assert [d["content"] for d in delivered] == ["Wait for the design decision."]


@pytest.mark.asyncio
async def test_free_operator_comment_on_done_card_still_delivered(
    fake_redis, make_board, make_agent, make_task,
):
    """2026-05-18 rule stays: after the approve, a normal operator comment on
    the done card still reaches the agent."""
    board, builder, task = await _builder_card(make_board, make_agent, make_task)
    await _decide(task.id, board.id, "approve", "Looks good",
                  actor_user_id=uuid.uuid4())
    assert await _poll(builder.id) == []

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(task_id=task.id, author_type="user",
                          comment_type="message", content="Please also fix the footer."))
        await s.commit()

    delivered = await _poll(builder.id)
    assert [d["content"] for d in delivered] == ["Please also fix the footer."]


@pytest.mark.asyncio
async def test_agent_approve_note_counts_as_reviewed(
    fake_redis, make_board, make_agent, make_task,
):
    """The approve note's own type must still mark its author as a reviewer
    of the card (work_context.agents_who_reviewed_card)."""
    from app.services.work_context import agents_who_reviewed_card

    board = await make_board(name="Who Board", slug=f"who-{uuid.uuid4().hex[:8]}")
    reviewer = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    task = await make_task(board_id=board.id, title="Agent-reviewed",
                           status="review", assigned_agent_id=reviewer.id)

    await _decide(task.id, board.id, "approve", "LGTM", actor_agent=reviewer)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert reviewer.id in await agents_who_reviewed_card(s, task.id)
