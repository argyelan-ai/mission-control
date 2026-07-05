"""Tests: user_test Gate nur bei Browser-relevanten Tasks."""
import uuid
import pytest
from unittest.mock import patch, AsyncMock
from sqlmodel.ext.asyncio.session import AsyncSession
from tests.conftest import test_engine
from app.models.task import Task


@pytest.mark.anyio
async def test_browser_parent_gets_user_test(make_board, make_agent, make_task):
    """Root-Task mit needs_browser=True + Children → user_test."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    parent = await make_task(
        board.id, title="Build Dashboard",
        status="review", assigned_agent_id=rex.id,
        needs_browser=True,
    )
    await make_task(board.id, title="Subtask", parent_task_id=parent.id, status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.handle_test_handoff", new_callable=AsyncMock) as mock_handoff:
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, parent.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Looks good",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "user_test", f"Expected user_test, got {task.status}"
            mock_handoff.assert_called_once()


@pytest.mark.anyio
async def test_non_browser_parent_skips_user_test(make_board, make_agent, make_task):
    """Root-Task OHNE needs_browser + Children → direkt done."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    parent = await make_task(
        board.id, title="Morning Briefing",
        status="review", assigned_agent_id=rex.id,
    )
    await make_task(board.id, title="Subtask", parent_task_id=parent.id, status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock):
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, parent.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Approved",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "done", f"Expected done, got {task.status}"
            assert task.completed_at is not None


@pytest.mark.anyio
async def test_visual_proof_gets_user_test(make_board, make_agent, make_task):
    """Root-Task mit delegation_type=visual_proof + Children → user_test."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    parent = await make_task(
        board.id, title="Redesign Page",
        status="review", assigned_agent_id=rex.id,
        delegation_type="visual_proof",
    )
    await make_task(board.id, title="Subtask", parent_task_id=parent.id, status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.handle_test_handoff", new_callable=AsyncMock) as mock_handoff:
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, parent.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Ship it",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "user_test"


@pytest.mark.anyio
async def test_single_task_goes_done(make_board, make_agent, make_task):
    """Einzeltask ohne Children → direkt done."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    task_obj = await make_task(
        board.id, title="Single Fix",
        status="review", assigned_agent_id=rex.id,
        needs_browser=True,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock):
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, task_obj.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="OK",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "done"


# ── Human-simulating E2E toggle (Migration 0142) ─────────────────────


@pytest.mark.anyio
async def test_e2e_flag_gates_single_task_without_children(make_board, make_agent, make_task):
    """e2e_test_required=True → user_test auch OHNE Children/needs_browser."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    task_row = await make_task(
        board.id, title="Ad-hoc Feature",
        status="review", assigned_agent_id=rex.id,
        e2e_test_required=True,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.handle_test_handoff", new_callable=AsyncMock) as mock_handoff:
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, task_row.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Looks good",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "user_test", f"Expected user_test, got {task.status}"
            mock_handoff.assert_called_once()


@pytest.mark.anyio
async def test_e2e_flag_blocks_when_no_tester(make_board, make_agent, make_task):
    """Explizit angefordertes E2E + kein Tester → blocked mit Operator-Blocker,
    NICHT still übersprungen (fail-loud)."""
    from sqlmodel import select
    from app.models.task import TaskComment

    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    # KEIN Tester-Agent auf dem Board.

    task_row = await make_task(
        board.id, title="Feature ohne Tester",
        status="review", assigned_agent_id=rex.id,
        e2e_test_required=True,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock):
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, task_row.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="Approved",
                actor_agent=rex,
            )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task_row.id)
        assert fresh.status == "blocked", f"Expected blocked, got {fresh.status}"
        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_row.id)
        )).all()
        blockers = [c for c in comments if c.comment_type == "blocker"]
        assert blockers and "kein Tester-Agent" in blockers[0].content


@pytest.mark.anyio
async def test_legacy_no_tester_still_skips_silently(make_board, make_agent, make_task):
    """Implizites Gate (needs_browser+Children) ohne Tester bleibt beim alten
    Verhalten: user_test, kein Block (kein neues Verhalten aufgezwungen)."""
    board = await make_board(require_review_before_done=True)
    rex = await make_agent(name="Rex", role="reviewer", board_id=board.id)

    parent = await make_task(
        board.id, title="Browser-Phase",
        status="review", assigned_agent_id=rex.id,
        needs_browser=True,
    )
    await make_task(board.id, title="Sub", parent_task_id=parent.id, status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock):
            from app.services.task_lifecycle import execute_review_decision
            task = await s.get(Task, parent.id)
            await execute_review_decision(
                session=s, task=task, board_id=board.id,
                decision="approve", comment_text="ok",
                actor_agent=rex,
            )
            await s.refresh(task)
            assert task.status == "user_test"


@pytest.mark.anyio
async def test_tester_message_uses_playwright_mcp(make_board, make_agent, make_task):
    """Tester-Directive nutzt den Playwright-MCP-Flow, nicht mehr dev-browser."""
    from app.services.dispatch_message_builder import _build_test_message

    board = await make_board()
    tester = await make_agent(name="Tester", role="tester", board_id=board.id)
    task_row = await make_task(
        board.id, title="E2E Ziel", status="user_test",
        acceptance_criteria="Login funktioniert; Formular speichert",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_row.id)
        agent = await s.get(type(tester), tester.id)
        msg = await _build_test_message(task, agent, s)

    assert "dev-browser" not in msg
    assert "browser_navigate" in msg and "browser_snapshot" in msg
    assert "browser_click" in msg and "browser_resize" in msg
    assert "Login funktioniert" in msg  # acceptance criteria als Flows
    assert "TEST_PASS" in msg and "TEST_FAIL" in msg


@pytest.mark.anyio
async def test_test_handoff_finds_tester(make_board, make_agent, make_task):
    """Regression fuer den Enum-Bug (String 'tester' → role.value-Crash):
    existiert ein Tester, wird er wirklich zugewiesen."""
    board = await make_board(require_review_before_done=True)
    tester = await make_agent(name="Tester", role="tester", board_id=board.id)
    task_row = await make_task(board.id, title="Handoff-Ziel", status="user_test")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.activity.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.auto_dispatch_task", new_callable=AsyncMock, create=True), \
             patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock):
            from app.services.task_lifecycle import handle_test_handoff
            task = await s.get(Task, task_row.id)
            result = await handle_test_handoff(s, task, board.id)
            assert result is not None and result.id == tester.id
            await s.refresh(task)
            assert task.assigned_agent_id == tester.id
            assert task.dispatch_intent == "test_handoff"
