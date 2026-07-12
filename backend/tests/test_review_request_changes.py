"""Bug C (2026-07-12): request_changes ghost-state regression tests.

Operator clicks "Change" on a Human-Review task with a comment -> task must
NEVER end up stuck as in_progress with no agent working it. Before the fix,
execute_review_decision optimistically set task.status="in_progress" and
handle_review_rejection silently `return None`-ed in two cases (no developer
reconstructable; developer found but dispatch currently blocked) without
persisting any further state change - the task then sat forever as an
unwatched in_progress-without-agent ghost. Both cases must now resolve to a
claimable `inbox` state with an explanatory system comment.

Also covers Bug A: the operator's request_changes comment must show up in
the rework re-dispatch message (comment_type="review" was never surfaced -
only comment_type="feedback"/agent-authored was).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_request_changes_no_developer_lands_in_inbox_not_ghost_in_progress(
    fake_redis, make_board, make_task,
):
    """(a) No developer reconstructable (no ActivityEvent history, no
    progress/resolution comments, subtask so no Board-Lead fallback) ->
    task must end on inbox + unassigned + a system comment + an explicit
    auto_dispatch_task kick (an unassigned inbox task is NOT self-collecting
    — task_runner/watchdog require assigned_agent_id IS NOT NULL, so nothing
    would ever pick it back up on its own), NEVER stuck as in_progress with
    assigned_agent_id=None."""
    from app.models.task import Task, TaskComment
    from app.services.task_lifecycle import execute_review_decision

    board = await make_board(name="Ghost-State Board", slug=f"gs-{uuid.uuid4().hex[:8]}")
    parent = await make_task(board_id=board.id, title="Parent Phase", status="in_progress")
    task = await make_task(
        board_id=board.id, title="Subtask under Human-Review",
        status="review", assigned_agent_id=None,
        human_review_required=True, parent_task_id=parent.id,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
            t = await s.get(Task, task.id)
            await execute_review_decision(
                s, t, board.id, "request_changes",
                "Bitte den Fehler in der Validierung beheben.",
                actor_user_id=uuid.uuid4(),
            )

    mock_dispatch.assert_called_once_with(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        # The core regression: must NOT be a ghost in_progress-without-agent.
        assert not (updated.status == "in_progress" and updated.assigned_agent_id is None), (
            "task stuck as ghost in_progress with no agent — Bug C regression"
        )
        assert updated.status == "inbox"
        assert updated.assigned_agent_id is None

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        system_comments = [
            c for c in comments
            if c.comment_type == "system" and "Board Lead" in c.content
        ]
        assert system_comments, "expected a system comment explaining the Lead-Triage handoff"


@pytest.mark.asyncio
async def test_request_changes_dispatch_blocked_lands_in_inbox_assigned_to_developer(
    fake_redis, make_board, make_task, make_agent,
):
    """(b) Developer IS found, but check_dispatch_allowed() vetoes the
    re-dispatch (paused/asleep/run_control) -> task must land on inbox
    WITH assigned_agent_id=developer + explanatory system comment, not a
    ghost in_progress."""
    from app.models.task import Task, TaskComment
    from app.services.task_lifecycle import execute_review_decision

    board = await make_board(name="Dispatch-Blocked Board", slug=f"db-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(name="Cody", role="developer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Human-Review task with known developer",
        status="review", assigned_agent_id=None, human_review_required=True,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=dev.id,
            comment_type="progress", content="Implementation done",
        ))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.services.operations.check_dispatch_allowed",
                   new=AsyncMock(return_value=(False, "agent paused"))):
            t = await s.get(Task, task.id)
            await execute_review_decision(
                s, t, board.id, "request_changes",
                "Bitte den Fehler in der Validierung beheben.",
                actor_user_id=uuid.uuid4(),
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert not (updated.status == "in_progress" and updated.assigned_agent_id != dev.id), (
            "task stuck as ghost in_progress not reflecting the blocked-dispatch outcome — Bug C regression"
        )
        assert updated.status == "inbox"
        assert updated.assigned_agent_id == dev.id

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task.id)
        )).all()
        system_comments = [
            c for c in comments
            if c.comment_type == "system" and "nicht erlaubt" in c.content
        ]
        assert system_comments, "expected a system comment explaining the dispatch-blocked reason"


@pytest.mark.asyncio
async def test_request_changes_success_path_unchanged(
    fake_redis, make_board, make_task, make_agent,
):
    """Regression guard: the normal case (developer found, dispatch
    allowed, not busy) still redispatches to inbox+assigned as before."""
    from app.models.task import Task, TaskComment
    from app.services.task_lifecycle import execute_review_decision

    board = await make_board(name="Happy Path Board", slug=f"hp-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(name="Cody", role="developer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Human-Review task, normal rework",
        status="review", assigned_agent_id=None, human_review_required=True,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=dev.id,
            comment_type="progress", content="Implementation done",
        ))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
            t = await s.get(Task, task.id)
            await execute_review_decision(
                s, t, board.id, "request_changes",
                "Bitte den Fehler in der Validierung beheben.",
                actor_user_id=uuid.uuid4(),
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert updated.status == "inbox"
        assert updated.assigned_agent_id == dev.id
        assert updated.dispatch_intent == "review_rework"
        assert updated.dispatched_at is None  # cleared for the ACK-timeout watchdog


@pytest.mark.asyncio
async def test_review_rework_dispatch_message_includes_operator_comment(fake_redis, make_board, make_task, make_agent):
    """Bug A: the operator's request_changes comment (comment_type='review')
    must show up in the rework re-dispatch message — before the fix only
    comment_type='feedback' (agent-authored) was surfaced, so the operator's
    'Change' comment was invisible to the redispatched agent."""
    from app.models.task import Task, TaskComment
    from app.services.task_context_builder import _load_dispatch_context
    from app.services.dispatch_message_builder import _format_dispatch_message

    board = await make_board(name="Rework Message Board", slug=f"rm-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(name="Cody", role="developer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Task needing rework",
        status="inbox", assigned_agent_id=dev.id,
        dispatch_intent="review_rework",
    )
    marker = "UNIQUE-OPERATOR-FEEDBACK-MARKER-4471"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="user",
            comment_type="review",
            content=f"not ship-ready: {marker} — bitte die Validierung reparieren.",
        ))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        a = await s.get(__import__("app.models.agent", fromlist=["Agent"]).Agent, dev.id)
        ctx = await _load_dispatch_context(t, a, s)
        msg = _format_dispatch_message(t, a, ctx)

    assert marker in msg, "operator's request_changes comment missing from the rework dispatch message"
    assert "Review-Feedback" in msg


@pytest.mark.asyncio
async def test_dispatch_message_omits_review_feedback_block_when_not_rework(
    fake_redis, make_board, make_task, make_agent,
):
    """Negative case: a comment_type='review' comment sitting on a task that
    is NOT a review_rework re-dispatch must not surface the
    'Review-Feedback' block — the block is specific to the rework path, not
    a general review-comment leak into every dispatch message."""
    from app.models.task import Task, TaskComment
    from app.services.task_context_builder import _load_dispatch_context
    from app.services.dispatch_message_builder import _format_dispatch_message

    board = await make_board(name="Not-Rework Board", slug=f"nr-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(name="Cody", role="developer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Regular task, not a rework re-dispatch",
        status="inbox", assigned_agent_id=dev.id,
        dispatch_intent=None,
    )
    marker = "UNRELATED-REVIEW-COMMENT-MARKER-9982"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="user",
            comment_type="review",
            content=f"not ship-ready: {marker} — unrelated leftover comment.",
        ))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        a = await s.get(__import__("app.models.agent", fromlist=["Agent"]).Agent, dev.id)
        ctx = await _load_dispatch_context(t, a, s)
        msg = _format_dispatch_message(t, a, ctx)

    assert "Review-Feedback" not in msg
    assert marker not in msg
