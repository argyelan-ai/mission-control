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


@pytest.mark.asyncio
async def test_dispatch_intent_resets_to_root_on_approve_to_done(
    fake_redis, make_board, make_task, make_agent,
):
    """Follow-up (PR #109 review, 2026-07-14), point 1: dispatch_intent is
    sticky — nothing ever reset it back off 'review_rework'. Sequence: a
    rework re-dispatch legitimately shows the Review-Feedback block (as
    above); once that review cycle concludes via approve->done, the label
    must be cleared so a LATER, unrelated re-render of this task's dispatch
    message (e.g. after being reopened) doesn't leak the stale block with a
    stale review comment.

    Sets the task back to 'review' directly (bypassing handle_review_handoff,
    which already clears the intent as a side effect) to isolate exactly the
    behavior under test: execute_review_decision's approve/done branch itself
    must reset dispatch_intent, independent of how the task got back to
    review."""
    from app.models.task import Task, TaskComment
    from app.services.task_lifecycle import execute_review_decision
    from app.services.task_context_builder import _load_dispatch_context
    from app.services.dispatch_message_builder import _format_dispatch_message
    from app.models.agent import Agent

    board = await make_board(name="Rework-Then-Done Board", slug=f"rtd-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(name="Cody", role="developer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Rework then clean close",
        status="review", assigned_agent_id=None, human_review_required=True,
    )
    marker = "STALE-REWORK-FEEDBACK-MARKER-7731"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Identifies Cody as the original developer for _find_last_developer,
        # same setup as test_request_changes_success_path_unchanged above.
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=dev.id,
            comment_type="progress", content="Implementation done",
        ))
        s.add(TaskComment(
            task_id=task.id, author_type="user", comment_type="review",
            content=f"not ship-ready: {marker} — fix the validation.",
        ))
        await s.commit()

    # 1) Reject → rework re-dispatch. Block-present behavior already covered
    # by test_review_rework_dispatch_message_includes_operator_comment; here
    # we only need dispatch_intent to land on "review_rework".
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            t = await s.get(Task, task.id)
            await execute_review_decision(
                s, t, board.id, "request_changes",
                "Bitte den Fehler in der Validierung beheben.",
                actor_user_id=uuid.uuid4(),
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        assert t.dispatch_intent == "review_rework"
        # 2) Back to review WITHOUT going through handle_review_handoff —
        # reproduces the worst case where nothing else has touched the label.
        t.status = "review"
        s.add(t)
        await s.commit()

    # 3) Approve → done. This is the fix under test.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.verticals.hooks.run_task_done_hooks", new_callable=AsyncMock):
            t = await s.get(Task, task.id)
            await execute_review_decision(
                s, t, board.id, "approve", "LGTM now.",
                actor_user_id=uuid.uuid4(),
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert updated.status == "done"
        assert updated.dispatch_intent == "root", (
            "dispatch_intent must reset off 'review_rework' once the review "
            "cycle concludes — otherwise it leaks into any later redispatch "
            "of this same task row"
        )

        # 4) Prove the leak is actually closed: rendering a dispatch message
        # for this task now must NOT show the Review-Feedback block, even
        # though the stale review comment is still sitting in the DB.
        a = await s.get(Agent, dev.id)
        updated.assigned_agent_id = dev.id  # as if freshly reactivated
        ctx = await _load_dispatch_context(updated, a, s)
        msg = _format_dispatch_message(updated, a, ctx)

    assert "Review-Feedback" not in msg
    assert marker not in msg


@pytest.mark.asyncio
async def test_self_reject_no_longer_leaves_ghost_in_progress(
    fake_redis, make_board, make_task, make_agent,
):
    """Follow-up (PR #109 review, 2026-07-14), point 2: same bug class as
    Bug C, just in the one branch Bug C's fix didn't reach. When the
    rejecting agent IS the original developer (e.g. a developer-role agent
    classified as its own reviewer), handle_review_rejection used to
    `return noop` and leave the task sitting in the caller's provisional
    in_progress status — untouched, no comment, no event, stale dispatch
    bookkeeping. Now it falls through to the same explicit
    inbox+event+redispatch treatment as every other developer."""
    from app.models.task import Task
    from app.models.activity import ActivityEvent
    from app.services.task_lifecycle import handle_review_rejection

    board = await make_board(name="Self-Reject Board", slug=f"sr-{uuid.uuid4().hex[:8]}")
    # Sole agent on the board, developer role, no dedicated reviewer role —
    # find_reviewer() returns None, so find_last_developer() does not
    # exclude this agent; it resolves as its own "original developer".
    cody = await make_agent(name="Cody", role="developer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Self-reviewed task",
        status="review", assigned_agent_id=cody.id,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(
            id=uuid.uuid4(), event_type="task.status_changed",
            title="Status change", board_id=board.id, task_id=task.id,
            agent_id=cody.id,
            detail={"old_status": "in_progress", "new_status": "review"},
        ))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
            t = await s.get(Task, task.id)
            result = await handle_review_rejection(
                s, t, board.id, rejecting_agent=cody,
            )

    assert result.outcome != "noop", "self-reject must not silently no-op"
    assert result.developer is not None
    assert result.developer.id == cody.id
    mock_dispatch.assert_called_once_with(task.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert not (updated.status == "in_progress"), (
            "self-reject must not leave the task as a ghost in_progress — "
            "same bug class as Bug C"
        )
        assert updated.status == "inbox"
        assert updated.assigned_agent_id == cody.id
        assert updated.dispatch_intent == "review_rework"

        from app.models.task import TaskEvent
        events = (await s.exec(
            select(TaskEvent).where(TaskEvent.task_id == task.id)
        )).all()
        assert any(e.to_status == "inbox" for e in events), (
            "expected an audit-trail TaskEvent for the self-reject redispatch"
        )


@pytest.mark.asyncio
async def test_tester_fail_via_generic_patch_endpoint_routes_to_developer(client, fake_redis):
    """Point 3a (test-gap follow-up): request_changes-shaped rejections are
    normally exercised via execute_review_decision (POST .../review). This
    test instead drives the generic PATCH /api/v1/agent/boards/{board}/tasks/
    {task} endpoint end-to-end (agent_task_status.py's `if new_status ==
    "in_progress" and old_status in ("review", "done", "user_test")` branch,
    ~line 2138) — the router wiring get_review_worker_agent_ids' docstring
    warns about ("an agent can't bypass the guard by using the generic PATCH
    endpoint") was never itself covered by an end-to-end test.

    Scenario: a TEST_FAIL outcome — the tester agent PATCHes status:
    in_progress on a user_test task (exactly the `reject_curl` step
    dispatch_message_builder._build_test_message tells testers to use on
    failure). old_status='user_test' is unconditionally routed to
    handle_review_rejection (the is_reviewer_ack carve-out only applies to
    old_status=='review'), so this is the real production path back to the
    developer for a failed E2E test."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Generic-PATCH Board", slug=f"gp-{board_id.hex[:8]}")
        s.add(board)

        dev_token_raw, dev_token_hash = generate_agent_token()
        dev = Agent(
            id=uuid.uuid4(), name="Cody", role="developer", board_id=board_id,
            agent_token_hash=dev_token_hash, scopes=["tasks:read", "tasks:write"],
        )
        s.add(dev)

        tester_token_raw, tester_token_hash = generate_agent_token()
        tester = Agent(
            id=uuid.uuid4(), name="Tess", role="tester", board_id=board_id,
            agent_token_hash=tester_token_hash, scopes=["tasks:read", "tasks:write"],
        )
        s.add(tester)
        await s.commit()
        await s.refresh(dev)
        await s.refresh(tester)

        task = Task(
            id=uuid.uuid4(), board_id=board_id, title="E2E test failed",
            status="user_test", assigned_agent_id=tester.id,
        )
        s.add(task)
        # Identifies Cody as the original developer for _find_last_developer.
        s.add(TaskComment(
            task_id=task.id, author_type="agent", author_agent_id=dev.id,
            comment_type="progress", content="Implementation done",
        ))
        await s.commit()
        await s.refresh(task)

    with patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {tester_token_raw}"},
        )

    assert resp.status_code == 200, resp.text
    mock_dispatch.assert_called_once_with(task.id, board_id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert updated.status == "inbox", (
            "must route through handle_review_rejection, not stay in_progress"
        )
        assert updated.assigned_agent_id == dev.id
        assert updated.dispatch_intent == "review_rework"


@pytest.mark.asyncio
async def test_rejection_queued_outcome_real_requeue(client, fake_redis, make_board, make_task, make_agent):
    """Point 3b (test-gap follow-up): the existing busy-dev queued-outcome
    test (test_workflow_scenarios.test_rejection_busy_dev_queues_task) mocks
    app.services.task_queue.enqueue_task, so it only proves the busy-check
    routing decision — never that the Redis requeue actually works. This
    test uses the `client` fixture (which wires fake_redis as the real
    get_redis() singleton) and lets enqueue_task run for real, then verifies
    the task is genuinely retrievable from the developer's Redis queue —
    the mechanism the watchdog relies on to redeliver it."""
    from app.models.activity import ActivityEvent
    from app.services.task_lifecycle import handle_review_rejection
    from app.services.task_queue import queue_length, dequeue_task

    board = await make_board(name="Real-Requeue Board", slug=f"rq-{uuid.uuid4().hex[:8]}")
    dev = await make_agent(name="Cody", role="developer", board_id=board.id)
    reviewer = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Rework while dev is busy",
        status="review", assigned_agent_id=reviewer.id,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task as TaskModel
        # Developer already has another active task → busy.
        s.add(TaskModel(
            id=uuid.uuid4(), board_id=board.id, title="Other active task",
            status="in_progress", assigned_agent_id=dev.id,
        ))
        s.add(ActivityEvent(
            id=uuid.uuid4(), event_type="task.status_changed",
            title="Status change", board_id=board.id, task_id=task.id,
            agent_id=dev.id,
            detail={"old_status": "in_progress", "new_status": "review"},
        ))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
            t = await s.get(Task, task.id)
            r = await s.get(type(reviewer), reviewer.id)
            result = await handle_review_rejection(s, t, board.id, rejecting_agent=r)

    assert result.outcome == "queued"
    assert result.developer.id == dev.id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, task.id)
        assert updated.status == "inbox"
        assert updated.dispatch_intent == "review_rework"

    # The real proof: the task is actually sitting in Cody's Redis queue,
    # not just marked inbox — the watchdog's requeue-drain reads from here.
    assert await queue_length(str(dev.id)) == 1
    dequeued_id = await dequeue_task(str(dev.id))
    assert dequeued_id == str(task.id)
    assert await queue_length(str(dev.id)) == 0
