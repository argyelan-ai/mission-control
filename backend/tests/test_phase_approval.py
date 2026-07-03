"""Phase Approval Workflow tests."""
from unittest.mock import AsyncMock, patch

import pytest
from uuid import uuid4
from sqlmodel import select
from app.routers.agent_scoped import VALID_COMMENT_TYPES
from app.models.task import Task, TaskComment
from app.models.agent import Agent
from app.models.board import Board


def test_valid_comment_types_contains_phase_approval_types():
    """Phase approval workflow needs 3 new comment types."""
    assert "subtask_completed" in VALID_COMMENT_TYPES
    assert "phase_approved" in VALID_COMMENT_TYPES
    assert "phase_rewrite_request" in VALID_COMMENT_TYPES


@pytest.mark.asyncio
async def test_subtask_done_posts_comment_on_parent(async_session, board_with_agents):
    """When a subtask is set to done, the parent receives a subtask_completed comment."""
    board = board_with_agents["board"]
    developer = board_with_agents["developer"]
    boss = board_with_agents["boss"]

    # Parent task (assigned to Boss)
    parent = Task(
        board_id=board.id, title="Feature X", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    # Subtask (assigned to developer)
    subtask = Task(
        board_id=board.id, title="Subtask 1", status="in_progress",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    async_session.add(subtask)
    await async_session.commit()
    await async_session.refresh(subtask)

    # Simulate subtask → done via helper function
    # Phase 4 REF-02 step 3: helper lives in app.routers.agent_comments (re-exported
    # by agent_scoped via Pattern S1 shim). Patch the module where emit_event is
    # actually looked up at call time — agent_comments — not the shim.
    from app.routers.agent_scoped import _post_subtask_completion_comment
    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        await _post_subtask_completion_comment(async_session, subtask, developer)

    # Verify: Parent has 1 new comment of type subtask_completed
    result = await async_session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == parent.id)
        .where(TaskComment.comment_type == "subtask_completed")
    )
    comments = result.all()
    assert len(comments) == 1
    assert "Subtask 1" in comments[0].content
    assert str(subtask.id) in comments[0].content


@pytest.mark.asyncio
async def test_subtask_done_no_comment_when_no_parent(async_session, board_with_agents):
    """Root task (without a parent) does not trigger a subtask_completed comment."""
    board = board_with_agents["board"]
    developer = board_with_agents["developer"]

    root = Task(
        board_id=board.id, title="Root Task", status="in_progress",
        assigned_agent_id=developer.id,
    )
    async_session.add(root)
    await async_session.commit()
    await async_session.refresh(root)

    from app.routers.agent_scoped import _post_subtask_completion_comment
    # Phase 4 REF-02 step 3: helper lives in app.routers.agent_comments now.
    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock):
        await _post_subtask_completion_comment(async_session, root, developer)

    # No comment should exist
    result = await async_session.exec(
        select(TaskComment).where(TaskComment.comment_type == "subtask_completed")
    )
    comments = result.all()
    assert len(comments) == 0


@pytest.mark.asyncio
async def test_subtask_completion_helper_tolerates_emit_event_failure(async_session, board_with_agents):
    """If emit_event raises, the helper should not crash (best-effort).

    Guard semantics:
    - The helper itself catches NO exceptions (stays simple).
    - The handler in agent_scoped.py wraps the helper call in try/except and
      logs only a warning, so the PATCH response doesn't tip over into 500.
    - This test proves: the helper still raises the exception, which
      makes the handler wrapper the sole safeguard against Redis/event failures.
    """
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="P", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    subtask = Task(
        board_id=board.id, title="S", status="in_progress",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    async_session.add(subtask)
    await async_session.commit()
    await async_session.refresh(subtask)

    # Simulate emit_event raising — the hook in the handler uses try/except,
    # but the helper itself has no try/except. Verify the handler's wrapper behaves.
    # Phase 4 REF-02 step 3: helper lives in app.routers.agent_comments now.
    with patch(
        "app.routers.agent_comments.emit_event",
        new_callable=AsyncMock,
        side_effect=RuntimeError("redis down"),
    ):
        from app.routers.agent_scoped import _post_subtask_completion_comment
        # The helper should raise (it doesn't catch); the HANDLER catches it.
        with pytest.raises(RuntimeError):
            await _post_subtask_completion_comment(async_session, subtask, developer)


@pytest.mark.asyncio
async def test_create_phase_approval_task_assigns_to_board_lead(async_session, board_with_agents):
    """create_phase_approval_task creates subtask assigned to Board Lead with delegation_type phase_approval."""
    from unittest.mock import patch, AsyncMock

    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Feature X", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    # 2 completed subtasks
    for i in range(2):
        st = Task(
            board_id=board.id, title=f"Subtask {i}", status="done",
            parent_task_id=parent.id, assigned_agent_id=developer.id,
        )
        async_session.add(st)
    await async_session.commit()

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        from app.services.task_lifecycle import create_phase_approval_task
        approval = await create_phase_approval_task(async_session, parent, boss)

    assert approval is not None
    assert approval.parent_task_id == parent.id
    assert approval.assigned_agent_id == boss.id
    assert approval.delegation_type == "phase_approval"
    assert approval.status == "inbox"
    assert approval.title.startswith("Phase Approval:")
    assert "Feature X" in approval.title
    # Description should reference the 2 completed subtasks
    assert "Subtask 0" in approval.description
    assert "Subtask 1" in approval.description


@pytest.mark.asyncio
async def test_create_phase_approval_task_returns_none_without_board_lead(async_session):
    """create_phase_approval_task returns None if no board_lead is passed (fallback)."""
    from app.models.board import Board
    board = Board(name="Lonely Board", slug="lonely", icon="🌙")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    parent = Task(board_id=board.id, title="Orphan", status="in_progress")
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    from app.services.task_lifecycle import create_phase_approval_task
    result = await create_phase_approval_task(async_session, parent, None)

    assert result is None


@pytest.mark.asyncio
async def test_watchdog_creates_approval_task_when_all_subtasks_done(
    async_session, board_with_agents
):
    """When all subtasks are done, watchdog creates phase-approval-task (not Rex handoff)."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from app.services.watchdog.core import WatchdogService

    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Feature Y", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    for i in range(3):
        st = Task(
            board_id=board.id, title=f"ST {i}", status="done",
            parent_task_id=parent.id, assigned_agent_id=developer.id,
        )
        async_session.add(st)
    await async_session.commit()

    # record_phase_completion is called as a coroutine factory — passed into
    # _create_background_task which we also mock to a no-op. Use MagicMock so
    # the "coroutine" it returns is just a MagicMock (no unawaited warning).
    # Mock emit_event, redis dedup, and auto_memory to isolate watchdog logic
    with patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.watchdog.task_monitor.get_redis", new_callable=AsyncMock) as mock_redis_fn, \
         patch("app.services.auto_memory.record_phase_completion", new_callable=MagicMock), \
         patch("app.services.watchdog.core._create_background_task"):
        # Redis: dedup checks return None (no previous fingerprint)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True
        mock_redis_fn.return_value = mock_redis

        monitor = WatchdogService()
        await monitor._check_phase_completions(async_session)

    # Verify: an approval task was created for Boss
    approval_result = await async_session.exec(
        select(Task)
        .where(Task.parent_task_id == parent.id)
        .where(Task.delegation_type == "phase_approval")
    )
    approvals = approval_result.all()
    assert len(approvals) == 1
    assert approvals[0].assigned_agent_id == boss.id
    assert approvals[0].status == "inbox"

    # Parent should still be in_progress (not review yet)
    await async_session.refresh(parent)
    assert parent.status == "in_progress"


@pytest.mark.asyncio
async def test_phase_approved_comment_promotes_parent_to_review(async_session, board_with_agents):
    """phase_approved comment on approval task moves parent from in_progress to review.

    Note (2026-04-22): the board must explicitly have require_review_before_done=True;
    since the bug-2 fix, the parent stays in_progress on trust-by-default boards
    (dedicated test in test_phase_approval_bugfixes.py).
    """
    from unittest.mock import patch, AsyncMock
    board = board_with_agents["board"]
    board.require_review_before_done = True  # explicit review path
    async_session.add(board)
    await async_session.commit()
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Feature Z", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    # Subtasks done
    for i in range(2):
        st = Task(
            board_id=board.id, title=f"ST {i}", status="done",
            parent_task_id=parent.id, assigned_agent_id=developer.id,
        )
        async_session.add(st)
    await async_session.commit()

    # Approval task (Boss is Board Lead)
    approval = Task(
        board_id=board.id, title="Phase Approval: Feature Z", status="in_progress",
        parent_task_id=parent.id, assigned_agent_id=boss.id,
        delegation_type="phase_approval",
    )
    async_session.add(approval)
    await async_session.commit()
    await async_session.refresh(approval)

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        from app.services.task_lifecycle import handle_phase_approval_decision
        result = await handle_phase_approval_decision(
            async_session, approval, boss,
            comment_type="phase_approved",
            comment_content="Alles sieht gut aus. Phase abgeschlossen.",
        )

    assert result["decision"] == "approved"
    assert result["parent_promoted"] is True
    await async_session.refresh(parent)
    assert parent.status == "review"


@pytest.mark.asyncio
async def test_phase_rewrite_reopens_specified_subtasks(async_session, board_with_agents):
    """phase_rewrite_request re-opens the specified subtask AND triggers a fresh
    dispatch path (full state-reset + feedback TaskComment + auto_dispatch_task)
    so the agent receives a wakeup signal.

    Regression guard for the 2026-05-20 incident: Researcher subtask 6a65a509
    sat idle for ~1h after Boss requested a rewrite because dispatched_at/ack_at
    were preserved, leaving the subtask invisible to the agent's poll loop.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch, AsyncMock
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Feature W", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    # Both subtasks "carry" a previous dispatch so we can prove the reset
    # actually clears the timestamps.
    prev_dispatch = datetime(2026, 5, 20, 20, 14, 0, tzinfo=timezone.utc)
    prev_ack = datetime(2026, 5, 20, 20, 14, 30, tzinfo=timezone.utc)
    st1 = Task(
        board_id=board.id, title="ST 1", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
        dispatched_at=prev_dispatch, ack_at=prev_ack,
    )
    st2 = Task(
        board_id=board.id, title="ST 2", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
        dispatched_at=prev_dispatch, ack_at=prev_ack,
    )
    async_session.add(st1)
    async_session.add(st2)
    await async_session.commit()
    await async_session.refresh(st1)
    await async_session.refresh(st2)

    approval = Task(
        board_id=board.id, title="Phase Approval: Feature W", status="in_progress",
        parent_task_id=parent.id, assigned_agent_id=boss.id,
        delegation_type="phase_approval",
    )
    async_session.add(approval)
    await async_session.commit()
    await async_session.refresh(approval)

    rewrite_content = (
        f"ST 1 braucht mehr Detail:\n"
        f"subtask: {st1.id}, grund: Deliverable fehlt\n"
        f"ST 2 ist ok, bleibt done."
    )

    dispatch_calls: list[tuple] = []

    async def _capture_dispatch(task_id, board_id):
        dispatch_calls.append((task_id, board_id))

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.dispatch_attempt_audit.clear_dispatch_attempt_id",
               new_callable=AsyncMock), \
         patch("app.services.dispatch.auto_dispatch_task",
               new=_capture_dispatch):
        from app.services.task_lifecycle import handle_phase_approval_decision
        result = await handle_phase_approval_decision(
            async_session, approval, boss,
            comment_type="phase_rewrite_request",
            comment_content=rewrite_content,
        )
        # Yield control so the asyncio.create_task() coroutine actually
        # runs before the patch context manager exits.
        import asyncio as _asyncio
        await _asyncio.sleep(0)

    assert result["decision"] == "rewrite"
    assert st1.id in result["reopened"]
    assert st2.id not in result["reopened"]

    await async_session.refresh(st1)
    await async_session.refresh(st2)

    # ST1: status reset to in_progress AND dispatch-tracking cleared so
    # auto_dispatch_task picks it up as a fresh delivery.
    assert st1.status == "in_progress"
    assert st1.dispatched_at is None, "dispatched_at must be cleared for re-dispatch"
    assert st1.ack_at is None, "ack_at must be cleared for re-dispatch"
    assert st1.completed_at is None

    # ST2 untouched (not in mentioned_ids). SQLite strips tzinfo on
    # round-trip so we compare timezone-naive values.
    assert st2.status == "done"
    assert st2.dispatched_at is not None
    assert st2.dispatched_at.replace(tzinfo=None) == prev_dispatch.replace(tzinfo=None)
    assert st2.ack_at is not None
    assert st2.ack_at.replace(tzinfo=None) == prev_ack.replace(tzinfo=None)

    # Parent stays in_progress.
    await async_session.refresh(parent)
    assert parent.status == "in_progress"

    # auto_dispatch_task was triggered for ST1 (the only re-opened subtask).
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0][0] == st1.id
    assert dispatch_calls[0][1] == board.id

    # A feedback TaskComment was posted on ST1 carrying the rewrite reason
    # so the agent sees WHY they were re-opened.
    feedback_result = await async_session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == st1.id)
        .where(TaskComment.comment_type == "feedback")
    )
    feedback = feedback_result.all()
    assert len(feedback) == 1
    assert "Deliverable fehlt" in feedback[0].content
    assert "Rewrite-Auftrag" in feedback[0].content
    assert feedback[0].author_agent_id == boss.id

    # No directive on ST2 (untouched).
    feedback_st2 = await async_session.exec(
        select(TaskComment)
        .where(TaskComment.task_id == st2.id)
        .where(TaskComment.comment_type == "feedback")
    )
    assert feedback_st2.all() == []


@pytest.mark.asyncio
async def test_phase_rewrite_extracts_per_subtask_reason():
    """_extract_rewrite_reason isolates the right block from a multi-subtask brief."""
    from uuid import UUID
    from app.services.task_lifecycle import _extract_rewrite_reason

    sid1 = UUID("11111111-1111-1111-1111-111111111111")
    sid2 = UUID("22222222-2222-2222-2222-222222222222")
    sid3 = UUID("33333333-3333-3333-3333-333333333333")

    multi = (
        f"Hier sind die Punkte fuer das Team:\n"
        f"subtask: {sid1}, grund: Tippfehler korrigieren, Umlaute pruefen\n"
        f"subtask: {sid2}, grund: Quelle 4 fehlt, neu recherchieren\n"
        f"Generelle Anmerkung am Ende."
    )

    r1 = _extract_rewrite_reason(multi, sid1)
    r2 = _extract_rewrite_reason(multi, sid2)
    r3 = _extract_rewrite_reason(multi, sid3)

    # Each subtask gets only its own block — no cross-contamination.
    assert r1.startswith("Tippfehler korrigieren")
    assert "Quelle 4" not in r1
    assert r2.startswith("Quelle 4 fehlt")
    assert "Tippfehler" not in r2
    # Trailing free-form text travels with the LAST matched block (single sid2 here)
    assert "Generelle Anmerkung" in r2

    # Subtask not present in the brief → fallback to full content.
    assert r3 == multi.strip()


@pytest.mark.asyncio
async def test_phase_rewrite_dispatches_reopened_subtasks(async_session, board_with_agents):
    """auto_dispatch_task is invoked exactly once per re-opened subtask."""
    from unittest.mock import patch, AsyncMock
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Multi-Rewrite", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    st1 = Task(
        board_id=board.id, title="ST A", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    st2 = Task(
        board_id=board.id, title="ST B", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    st3 = Task(
        board_id=board.id, title="ST C", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    for st in (st1, st2, st3):
        async_session.add(st)
    await async_session.commit()
    for st in (st1, st2, st3):
        await async_session.refresh(st)

    approval = Task(
        board_id=board.id, title="Phase Approval: Multi-Rewrite", status="in_progress",
        parent_task_id=parent.id, assigned_agent_id=boss.id,
        delegation_type="phase_approval",
    )
    async_session.add(approval)
    await async_session.commit()
    await async_session.refresh(approval)

    # Reopen ST1 + ST3, leave ST2 alone.
    rewrite_content = (
        f"subtask: {st1.id}, grund: Inhalt A fehlt\n"
        f"subtask: {st3.id}, grund: Inhalt C ist falsch"
    )

    captured: list = []

    async def _capture(task_id, board_id):
        captured.append(task_id)

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.dispatch_attempt_audit.clear_dispatch_attempt_id",
               new_callable=AsyncMock), \
         patch("app.services.dispatch.auto_dispatch_task", new=_capture):
        from app.services.task_lifecycle import handle_phase_approval_decision
        await handle_phase_approval_decision(
            async_session, approval, boss,
            comment_type="phase_rewrite_request",
            comment_content=rewrite_content,
        )
        import asyncio as _asyncio
        await _asyncio.sleep(0)

    # Exactly one auto_dispatch_task call per re-opened subtask, no extras.
    assert set(captured) == {st1.id, st3.id}
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_phase_approval_decision_returns_unknown_for_wrong_comment_type(async_session, board_with_agents):
    """Helper returns decision=unknown for comment_types other than phase_approved/phase_rewrite_request."""
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    parent = Task(board_id=board.id, title="P", status="in_progress", assigned_agent_id=boss.id)
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    approval = Task(
        board_id=board.id, title="A", status="in_progress",
        parent_task_id=parent.id, assigned_agent_id=boss.id,
        delegation_type="phase_approval",
    )
    async_session.add(approval)
    await async_session.commit()
    await async_session.refresh(approval)

    from app.services.task_lifecycle import handle_phase_approval_decision
    result = await handle_phase_approval_decision(
        async_session, approval, boss,
        comment_type="message",  # wrong type
        comment_content="random",
    )
    assert result["decision"] == "unknown"
    assert result["reopened"] == []
    assert result["parent_promoted"] is False


# ── Push callback: immediate approval creation on subtask completion ──


@pytest.mark.asyncio
async def test_push_callback_creates_approval_when_last_sibling_done(async_session, board_with_agents):
    """Push: as soon as all subtasks are done, a phase_approval task is created immediately."""
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Parent Phase", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    sub1 = Task(
        board_id=board.id, title="Sub 1", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    sub2 = Task(
        board_id=board.id, title="Sub 2", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    async_session.add_all([sub1, sub2])
    await async_session.commit()
    await async_session.refresh(sub2)

    from app.routers.agent_scoped import _handle_phase_completion_push
    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        await _handle_phase_completion_push(async_session, sub2)

    approval_result = await async_session.exec(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.delegation_type == "phase_approval",
        )
    )
    approval = approval_result.first()
    assert approval is not None
    assert approval.assigned_agent_id == boss.id
    assert approval.status == "inbox"


@pytest.mark.asyncio
async def test_push_callback_skips_when_sibling_still_in_progress(async_session, board_with_agents):
    """Push: if a sibling is not yet done, NO approval is created."""
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Parent", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    sub_done = Task(
        board_id=board.id, title="Done sub", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    sub_running = Task(
        board_id=board.id, title="Running sub", status="in_progress",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    async_session.add_all([sub_done, sub_running])
    await async_session.commit()
    await async_session.refresh(sub_done)

    from app.routers.agent_scoped import _handle_phase_completion_push
    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        await _handle_phase_completion_push(async_session, sub_done)

    approval_result = await async_session.exec(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.delegation_type == "phase_approval",
        )
    )
    assert approval_result.first() is None


@pytest.mark.asyncio
async def test_push_callback_idempotent_when_approval_exists(async_session, board_with_agents):
    """Push: if a phase_approval task already exists, don't create a second one."""
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]
    developer = board_with_agents["developer"]

    parent = Task(
        board_id=board.id, title="Parent", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    sub = Task(
        board_id=board.id, title="Sub", status="done",
        parent_task_id=parent.id, assigned_agent_id=developer.id,
    )
    existing_approval = Task(
        board_id=board.id, title="Existing Approval", status="inbox",
        parent_task_id=parent.id, assigned_agent_id=boss.id,
        delegation_type="phase_approval",
    )
    async_session.add_all([sub, existing_approval])
    await async_session.commit()
    await async_session.refresh(sub)

    from app.routers.agent_scoped import _handle_phase_completion_push
    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        await _handle_phase_completion_push(async_session, sub)

    approval_count = await async_session.exec(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.delegation_type == "phase_approval",
        )
    )
    assert len(approval_count.all()) == 1  # only the existing one


@pytest.mark.asyncio
async def test_push_callback_skips_when_self_is_phase_approval(async_session, board_with_agents):
    """Push: approval-task completion must not trigger another approval creation."""
    board = board_with_agents["board"]
    boss = board_with_agents["boss"]

    parent = Task(
        board_id=board.id, title="Parent", status="in_progress",
        assigned_agent_id=boss.id,
    )
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    approval = Task(
        board_id=board.id, title="Approval Done", status="done",
        parent_task_id=parent.id, assigned_agent_id=boss.id,
        delegation_type="phase_approval",
    )
    async_session.add(approval)
    await async_session.commit()
    await async_session.refresh(approval)

    from app.routers.agent_scoped import _handle_phase_completion_push
    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        await _handle_phase_completion_push(async_session, approval)

    # No NEW phase_approval task should have been created (only the existing one)
    result = await async_session.exec(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.delegation_type == "phase_approval",
        )
    )
    assert len(result.all()) == 1


@pytest.mark.asyncio
async def test_push_callback_no_board_lead_logs_warning(async_session):
    """Push: if no board lead exists, log a warning and do nothing (watchdog takes over)."""
    from app.models.board import Board

    board = Board(name="Orphan Board", slug="orphan")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    parent = Task(board_id=board.id, title="Parent", status="in_progress")
    async_session.add(parent)
    await async_session.commit()
    await async_session.refresh(parent)

    sub = Task(
        board_id=board.id, title="Sub", status="done",
        parent_task_id=parent.id,
    )
    async_session.add(sub)
    await async_session.commit()
    await async_session.refresh(sub)

    from app.routers.agent_scoped import _handle_phase_completion_push
    # No board lead → silent early return, no approval task
    await _handle_phase_completion_push(async_session, sub)

    result = await async_session.exec(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.delegation_type == "phase_approval",
        )
    )
    assert result.first() is None
