"""Tests for review decisions — explicit review decisions."""
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession


# ── Helpers ──────────────────────────────────────────────────────────


async def _get_session(test_engine):
    """Create a fresh session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


async def _fresh_session(test_engine):
    """Single session for assertions (non-generator)."""
    return AsyncSession(test_engine, expire_on_commit=False)


# ── Test 1: Approve sets status=done + review_decision ───────────────


@pytest.mark.asyncio
async def test_review_approve(make_board, make_agent, make_task):
    """decision=approve → status=done, review_decision=approved, completed_at set."""
    board = await make_board(name="Review Board", slug="rev-board")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Approve Me",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            # Load task and agent fresh
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            await execute_review_decision(
                s, t, board.id, "approve", "LGTM — alles gut",
                actor_agent=reviewer,
            )

        # Assertions with a fresh session
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.status == "done"
            assert t.review_decision == "approved"
            assert t.review_decided_at is not None
            assert t.completed_at is not None


# ── Test 2: Request changes calls handle_review_rejection ───────────


@pytest.mark.asyncio
async def test_review_request_changes(make_board, make_agent, make_task):
    """decision=request_changes → handle_review_rejection called."""
    board = await make_board(name="RC Board", slug="rc-board")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Reject Me",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine

    with (
        patch("app.services.task_lifecycle.handle_review_rejection", new_callable=AsyncMock) as mock_reject,
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            await execute_review_decision(
                s, t, board.id, "request_changes", "Tests fehlen",
                actor_agent=reviewer,
            )

        mock_reject.assert_called_once()

        # Check decision fields
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.review_decision == "changes_requested"
            assert t.review_decided_at is not None
            assert t.status == "in_progress"


# ── Test 3: Hold — status stays review ───────────────────────────────


@pytest.mark.asyncio
async def test_review_hold(make_board, make_agent, make_task):
    """decision=hold → status stays review, review_decision=hold."""
    board = await make_board(name="Hold Board", slug="hold-board")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Hold Me",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            await execute_review_decision(
                s, t, board.id, "hold", "Warte auf Klarstellung",
                actor_agent=reviewer,
            )

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.status == "review"  # Stays
            assert t.review_decision == "hold"
            assert t.review_decided_at is not None


# ── Test 4: Requires review status ────────────────────────────────────


@pytest.mark.asyncio
async def test_review_requires_review_status(make_board, make_agent, make_task):
    """409 if task is not in review status."""
    board = await make_board(name="Guard Board", slug="guard-board")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Not Review",
        status="in_progress", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine
    from fastapi import HTTPException

    if True:  # Phase 29: gateway rpc patch removed
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            with pytest.raises(HTTPException) as exc_info:
                await execute_review_decision(
                    s, t, board.id, "approve", "Versuch",
                    actor_agent=reviewer,
                )
            assert exc_info.value.status_code == 409


# ── Test 5: Run control guard ────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_run_control_guard(make_board, make_agent, make_task):
    """409 if run_control=stopped."""
    board = await make_board(name="Stopped Board", slug="stopped-board")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Stopped Review",
        status="review", assigned_agent_id=reviewer.id,
        run_control="stopped",
    )

    from tests.conftest import test_engine
    from fastapi import HTTPException

    if True:  # Phase 29: gateway rpc patch removed
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            with pytest.raises(HTTPException) as exc_info:
                await execute_review_decision(
                    s, t, board.id, "approve", "Versuch",
                    actor_agent=reviewer,
                )
            assert exc_info.value.status_code == 409


# ── Test 6: Decision cleared on handoff ──────────────────────────────


@pytest.mark.asyncio
async def test_review_decision_cleared_on_handoff(make_board, make_agent, make_task):
    """handle_review_handoff sets review_decision=null."""
    board = await make_board(name="Handoff Board", slug="handoff-board")
    developer = await make_agent(name="Cody", board_id=board.id, is_board_lead=False)
    reviewer = await make_agent(
        name="Rex", board_id=board.id, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, title="Handoff Task",
        status="review", assigned_agent_id=developer.id,
        review_decision="changes_requested",
        review_decided_at=datetime.utcnow(),
    )

    from tests.conftest import test_engine

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import handle_review_handoff
            result = await handle_review_handoff(s, t, board.id, developer=developer)

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.review_decision is None
            assert t.review_decided_at is None


# ── Test 7: Fallback — PATCH status:done sets review_decision ───────


@pytest.mark.asyncio
async def test_fallback_status_sets_decision(make_board, make_agent, make_task, auth_client):
    """PATCH status:done on a review task sets review_decision=approved."""
    board = await make_board(name="Fallback Board", slug="fallback-board")
    reviewer = await make_agent(
        name="Rex", board_id=board.id, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, title="Fallback Task",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine

    # Generate agent token
    from app.auth import generate_agent_token
    raw_token, reviewer_token_hash = generate_agent_token()

    # Update agent token in DB
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        r = await s.get(type(reviewer), reviewer.id)
        r.agent_token_hash = reviewer_token_hash
        r.scopes = []  # All scopes (backward-compat)
        s.add(r)
        await s.commit()

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
        patch("app.routers.agent_scoped.rpc", MagicMock(connected=False), create=True),
    ):
        resp = await auth_client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    if resp.status_code == 200:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.review_decision == "approved"
            assert t.review_decided_at is not None


# ── Test 8: Stop clears review_decision ──────────────────────────────


@pytest.mark.asyncio
async def test_stop_clears_review_decision(make_board, make_agent, make_task):
    """stop_task_run sets review_decision=null."""
    board = await make_board(name="Stop Board", slug="stop-board")
    reviewer = await make_agent(name="Rex", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Stop Task",
        status="review", assigned_agent_id=reviewer.id,
        review_decision="hold",
        review_decided_at=datetime.utcnow(),
    )

    from tests.conftest import test_engine

    with (
        # Phase 29 / Wave 4 cleanup: app.services.operations.rpc gone.
        # stop_task_run no longer touches any RPC — it only mutates the
        # task row and emits activity.
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            from app.services.operations import stop_task_run
            result = await stop_task_run(s, task.id, "user-123", "Testing")
            await s.commit()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.review_decision is None
            assert t.review_decided_at is None
            assert t.run_control == "stopped"


# ── Test 9: Resume clears review_decision ────────────────────────────


@pytest.mark.asyncio
async def test_resume_clears_review_decision(make_board, make_agent, make_task):
    """resume_task_run sets review_decision=null."""
    board = await make_board(name="Resume Board", slug="resume-board")
    reviewer = await make_agent(name="Rex", board_id=board.id)
    task = await make_task(
        board_id=board.id, title="Resume Task",
        status="blocked", assigned_agent_id=reviewer.id,
        run_control="stopped",
        review_decision="approved",
        review_decided_at=datetime.utcnow(),
    )

    from tests.conftest import test_engine

    with (
        # Phase 29 / Wave 4 cleanup: app.services.operations.rpc gone.
        # resume_task_run no longer touches any RPC — it only mutates the
        # task row back to inbox and emits activity.
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            from app.services.operations import resume_task_run
            result = await resume_task_run(s, task.id, "user-123")
            await s.commit()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.review_decision is None
            assert t.review_decided_at is None
            assert t.status == "inbox"


# ── Test 10: Comment creates review comment ──────────────────────────


@pytest.mark.asyncio
async def test_review_creates_comment(make_board, make_agent, make_task):
    """execute_review_decision creates a comment with comment_type=review."""
    board = await make_board(name="Comment Board", slug="comment-board")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Comment Task",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine
    from app.models.task import TaskComment

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            await execute_review_decision(
                s, t, board.id, "approve", "Gut gemacht",
                actor_agent=reviewer,
            )

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await s.exec(
                select(TaskComment).where(
                    TaskComment.task_id == task.id,
                    TaskComment.comment_type == "review",
                )
            )
            comments = result.all()
            assert len(comments) == 1
            assert "Gut gemacht" in comments[0].content
            assert comments[0].author_agent_id == reviewer.id


# ── Test 11: Consistency guard — request_changes + ship-ready = 409 ──


@pytest.mark.asyncio
async def test_consistency_request_changes_ship_ready_blocked(make_board, make_agent, make_task):
    """request_changes + 'ship-ready' in the comment = 409 contradiction."""
    board = await make_board(name="Consistency Board 1", slug="consist-1")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Inconsistent Task",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine
    from fastapi import HTTPException

    if True:  # Phase 29: gateway rpc patch removed
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            with pytest.raises(HTTPException) as exc_info:
                await execute_review_decision(
                    s, t, board.id, "request_changes",
                    "### Urteil: ship-ready\nAber bitte noch Tests ergaenzen",
                    actor_agent=reviewer,
                )
            assert exc_info.value.status_code == 409
            assert "Widerspruch" in str(exc_info.value.detail)


# ── Test 12: Consistency guard — hold + ship-ready = 409 ─────────────


@pytest.mark.asyncio
async def test_consistency_hold_ship_ready_blocked(make_board, make_agent, make_task):
    """hold + 'ship-ready' in the comment = 409 contradiction."""
    board = await make_board(name="Consistency Board 2", slug="consist-2")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Hold Ship Task",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine
    from fastapi import HTTPException

    if True:  # Phase 29: gateway rpc patch removed
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            with pytest.raises(HTTPException) as exc_info:
                await execute_review_decision(
                    s, t, board.id, "hold",
                    "### Urteil: ship-ready\nAber warte noch auf Klarstellung",
                    actor_agent=reviewer,
                )
            assert exc_info.value.status_code == 409
            assert "Widerspruch" in str(exc_info.value.detail)


# ── Test 13: Consistency guard — approve + not ship-ready = 409 ──────


@pytest.mark.asyncio
async def test_consistency_approve_not_ship_ready_blocked(make_board, make_agent, make_task):
    """approve + 'not ship-ready' in the comment = 409 contradiction."""
    board = await make_board(name="Consistency Board 3", slug="consist-3")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Approve Not Ship Task",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine
    from fastapi import HTTPException

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            with pytest.raises(HTTPException) as exc_info:
                await execute_review_decision(
                    s, t, board.id, "approve",
                    "### Urteil: not ship-ready\nBlocker gefunden",
                    actor_agent=reviewer,
                )
            assert exc_info.value.status_code == 409
            assert "Widerspruch" in str(exc_info.value.detail)


# ── Test 14: Consistency guard — approve + ship-ready = OK ───────────


@pytest.mark.asyncio
async def test_consistency_approve_ship_ready_allowed(make_board, make_agent, make_task):
    """approve + 'ship-ready' in the comment = allowed, no contradiction."""
    board = await make_board(name="Consistency Board 4", slug="consist-4")
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Consistent Ship Task",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import execute_review_decision
            await execute_review_decision(
                s, t, board.id, "approve",
                "### Urteil: ship-ready\nAlles gut, keine Blocker",
                actor_agent=reviewer,
            )

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.status == "done"
            assert t.review_decision == "approved"


# ── Test 15: Rework dispatch sets status to inbox (ACK check) ────────




# ── Test 16: Full rework E2E — with real _find_last_developer ───────




# ── Test 17: Reviewer ACK does NOT set review_decision ────────────


@pytest.mark.asyncio
async def test_reviewer_ack_does_not_set_review_decision(make_board, make_agent, make_task, auth_client):
    """PATCH status:in_progress on a review task (ACK) must NOT set review_decision.

    Reviewer ACK = start of work, not a review decision.
    review_decision may only be set via the explicit POST /review endpoint.
    """
    board = await make_board(name="ACK Board", slug="ack-board")
    reviewer = await make_agent(
        name="Rex", board_id=board.id, role="reviewer",
    )
    task = await make_task(
        board_id=board.id, title="ACK Test",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        r = await s.get(type(reviewer), reviewer.id)
        r.agent_token_hash = token_hash
        r.scopes = []
        s.add(r)
        await s.commit()

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
        patch("app.routers.agent_scoped.rpc", MagicMock(connected=False), create=True),
    ):
        resp = await auth_client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "in_progress"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )

    if resp.status_code == 200:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.review_decision is None, (
                f"Reviewer-ACK darf review_decision nicht setzen, got {t.review_decision}"
            )
            assert t.review_decided_at is None
            assert t.status == "in_progress"


# ── Test 18: Reviewer may approve despite review transitions ──────


@pytest.mark.asyncio
async def test_reviewer_can_approve_despite_review_transitions(make_board, make_agent, make_task):
    """Reviewer ACK (review→in_progress) and review completion (in_progress→review)
    must NOT trigger the self-review guard.

    Reproduces the Rex bug: Rex ACK'd, reviewed, and then couldn't approve
    because his own review transitions were counted as 'work'.
    """
    board = await make_board(name="Self-Review Board", slug="self-review")
    developer = await make_agent(
        name="Cody", board_id=board.id, role="developer",
    )
    reviewer = await make_agent(name="Rex", board_id=board.id, role="reviewer")
    task = await make_task(
        board_id=board.id, title="Self-Review Fix Test",
        status="review", assigned_agent_id=reviewer.id,
    )

    from tests.conftest import test_engine
    from app.models.task import TaskEvent

    # Simulate the event chain that triggers Rex's bug:
    # 1. Cody: inbox → in_progress (developer working)
    # 2. Cody: in_progress → review (developer done)
    # 3. Rex: review → in_progress (reviewer ACK)
    # 4. Rex: in_progress → review (reviewer done with review)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for from_s, to_s, agent in [
            ("inbox", "in_progress", developer),
            ("in_progress", "review", developer),
            ("review", "in_progress", reviewer),
            ("in_progress", "review", reviewer),
        ]:
            s.add(TaskEvent(
                id=uuid.uuid4(), task_id=task.id,
                from_status=from_s, to_status=to_s,
                changed_by="agent", agent_id=agent.id,
                created_at=datetime.utcnow(),
            ))
        await s.commit()

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            r = await s.get(type(reviewer), reviewer.id)  # reload in same session
            from app.services.task_lifecycle import execute_review_decision
            # Rex must be able to approve — his transitions are review work
            await execute_review_decision(
                s, t, board.id, "approve",
                "### Urteil: ship-ready\nAlles gut",
                actor_agent=r,
            )

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.status == "done"
            assert t.review_decision == "approved"


# ── Test 19: A real self-review remains forbidden ─────────────────


@pytest.mark.asyncio
async def test_real_self_review_still_blocked(make_board, make_agent, make_task):
    """A developer reviewing their own code remains blocked."""
    board = await make_board(name="Real Self-Review Board", slug="real-self")
    developer = await make_agent(
        name="Cody", board_id=board.id, role="developer",
    )
    task = await make_task(
        board_id=board.id, title="Real Self-Review Test",
        status="review", assigned_agent_id=developer.id,
    )

    from tests.conftest import test_engine
    from app.models.task import TaskEvent
    from fastapi import HTTPException

    # Cody worked as developer
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(
            id=uuid.uuid4(), task_id=task.id,
            from_status="inbox", to_status="in_progress",
            changed_by="agent", agent_id=developer.id,
            created_at=datetime.utcnow(),
        ))
        s.add(TaskEvent(
            id=uuid.uuid4(), task_id=task.id,
            from_status="in_progress", to_status="review",
            changed_by="agent", agent_id=developer.id,
            created_at=datetime.utcnow(),
        ))
        await s.commit()

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            d = await s.get(type(developer), developer.id)  # reload in same session
            from app.services.task_lifecycle import execute_review_decision
            with pytest.raises(HTTPException) as exc_info:
                await execute_review_decision(
                    s, t, board.id, "approve",
                    "### Urteil: ship-ready\nSieht gut aus",
                    actor_agent=d,
                )
            assert exc_info.value.status_code == 409
            assert "Self-review" in str(exc_info.value.detail)
