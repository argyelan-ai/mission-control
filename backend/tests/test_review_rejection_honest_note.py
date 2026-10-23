"""Round-7 re-check t1 NICE-2: "Request changes" tells what really happens.

The case: the builder is paused. A paused agent gets no push, but its own
poll still takes a card assigned to it by name (rule
R-paused-poll-takes-assigned, test_paused_agent_poll_claim.py). Live on
23.10. the builder took the rework 2 s after the click. The system note said
"Dispatch not allowed ... waits in the inbox", the activity said "blocked",
and the status event read ``in_progress → inbox`` although the card stood in
``review`` (the review decision sets a provisional in_progress first).

Now:
- the event names the real step, ``review → inbox``, in every outcome of the
  operator's review decision;
- a paused builder gets its own reason (``review_rejection_agent_paused``)
  and a note that says it takes the card itself — no "blocked", no raw
  ``PAUSED``;
- any other refusal (held card, halted system, ...) keeps the honest
  "not allowed right now, waits in the inbox" note.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _review_card_with_builder(make_board, make_task, make_agent, *, paused: bool):
    from app.models.task import TaskComment

    board = await make_board(name="Rework Board", slug=f"rw-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(
        name="Builder", role="developer", board_id=board.id,
        operational_mode="paused" if paused else "active",
    )
    task = await make_task(
        board_id=board.id, title="Mini page", status="review",
        assigned_agent_id=None, human_review_required=True,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=dev.id,
            comment_type="progress", content="Page built",
        ))
        await s.commit()
    return board, dev, task


async def _request_changes(task, board, emit):
    from app.models.task import Task
    from app.services.task_lifecycle import execute_review_decision

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", emit), \
             patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            t = await s.get(Task, task.id)
            await execute_review_decision(
                s, t, board.id, "request_changes", "Add one line.",
                actor_user_id=uuid.uuid4(),
            )


async def _events_and_notes(task):
    from app.models.task import TaskComment, TaskEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        events = (await s.exec(
            select(TaskEvent).where(TaskEvent.task_id == task.id)
        )).all()
        notes = [
            c.content for c in (await s.exec(
                select(TaskComment).where(TaskComment.task_id == task.id)
            )).all()
            if c.author_type == "system"
        ]
    return events, notes


@pytest.mark.asyncio
async def test_paused_builder_note_says_it_takes_the_card_itself(
    fake_redis, make_board, make_task, make_agent,
):
    board, dev, task = await _review_card_with_builder(make_board, make_task, make_agent, paused=True)
    emit = AsyncMock()
    await _request_changes(task, board, emit)

    events, notes = await _events_and_notes(task)
    [event] = events
    assert (event.from_status, event.to_status) == ("review", "inbox")
    assert event.reason == "review_rejection_agent_paused"

    [note] = notes
    assert "Builder" in note and "pausiert" in note and "holt" in note
    for wrong in ("nicht erlaubt", "blockiert", "PAUSED", "fuer"):
        assert wrong not in note, note

    rejected = [c for c in emit.await_args_list if c.args[1] == "task.review_rejected"]
    [call] = rejected
    title = call.args[2]
    assert "blockiert" not in title and "PAUSED" not in title, title
    assert call.kwargs.get("severity", "info") == "info"


@pytest.mark.asyncio
async def test_other_refusal_keeps_the_waiting_note_and_names_review(
    fake_redis, make_board, make_task, make_agent,
):
    board, _dev, task = await _review_card_with_builder(make_board, make_task, make_agent, paused=False)
    with patch("app.services.operations.check_dispatch_allowed",
               new=AsyncMock(return_value=(False, "Task run_control: manual_hold"))):
        await _request_changes(task, board, AsyncMock())

    events, notes = await _events_and_notes(task)
    [event] = events
    assert (event.from_status, event.to_status) == ("review", "inbox")
    assert event.reason == "review_rejection_dispatch_blocked"
    [note] = notes
    assert "nicht erlaubt" in note and "Inbox" in note


@pytest.mark.asyncio
async def test_redispatch_event_names_review_as_the_step_before(
    fake_redis, make_board, make_task, make_agent,
):
    board, _dev, task = await _review_card_with_builder(make_board, make_task, make_agent, paused=False)
    with patch("app.services.operations.check_dispatch_allowed",
               new=AsyncMock(return_value=(True, "ok"))):
        await _request_changes(task, board, AsyncMock())

    events, _notes = await _events_and_notes(task)
    [event] = events
    assert (event.from_status, event.to_status) == ("review", "inbox")
    assert event.reason == "review_rejection_redispatch"
