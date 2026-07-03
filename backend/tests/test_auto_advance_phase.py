"""Tests for watchdog auto-advance: phase done → next phase starts automatically."""

import uuid
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_fake_redis():
    """Fresh fakeredis instance for each test."""
    server = fakeredis.aioredis.FakeServer()
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


async def _create_project(session: AsyncSession, board_id: uuid.UUID, name: str = "Test Projekt"):
    from app.models.board import Project
    project = Project(
        id=uuid.uuid4(),
        board_id=board_id,
        name=name,
        status="active",
        project_type="feature",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _create_phase(
    session: AsyncSession,
    board_id: uuid.UUID,
    project_id: uuid.UUID,
    title: str,
    sort_order: int,
    status: str = "inbox",
):
    from app.models.task import Task
    phase = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        project_id=project_id,
        title=title,
        sort_order=sort_order,
        status=status,
        parent_task_id=None,
    )
    session.add(phase)
    await session.commit()
    await session.refresh(phase)
    return phase


async def _create_subtask(
    session: AsyncSession,
    board_id: uuid.UUID,
    parent_task_id: uuid.UUID,
    title: str,
    status: str = "done",
    project_id: uuid.UUID | None = None,
    assigned_agent_id: uuid.UUID | None = None,
):
    from app.models.task import Task
    task = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        parent_task_id=parent_task_id,
        project_id=project_id,
        title=title,
        status=status,
        assigned_agent_id=assigned_agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _patch_redis():
    """Patch get_redis in the task_monitor module."""
    fake = _make_fake_redis()

    async def _get():
        return fake

    return patch("app.services.watchdog.task_monitor.get_redis", new=_get)


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_auto_advance_next_phase(mock_emit, session, make_board):
    """Phase 1 done → parent stays in_progress, phase 2 starts automatically."""
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    board = await make_board(name="Auto Board", slug="auto-board", auto_dispatch_enabled=True)
    project = await _create_project(session, board.id)

    # Phase 1: in_progress with all subtasks done
    phase1 = await _create_phase(session, board.id, project.id, "Phase 1", sort_order=1, status="in_progress")
    await _create_subtask(session, board.id, phase1.id, "Sub 1.1", status="done", project_id=project.id)
    await _create_subtask(session, board.id, phase1.id, "Sub 1.2", status="done", project_id=project.id)

    # Phase 2: inbox, waiting
    phase2 = await _create_phase(session, board.id, project.id, "Phase 2", sort_order=2, status="inbox")

    monitor = TaskMonitorMixin()
    with _patch_redis():
        with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
            await monitor._check_phase_completions(session)

    # Phase 1 goes to review (phase review before auto-advance)
    await session.refresh(phase1)
    assert phase1.status == "review"

    # Phase 2 stays inbox (auto-advance only after phase 1 done)
    await session.refresh(phase2)
    assert phase2.status == "inbox"


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_auto_advance_no_next_phase(mock_emit, session, make_board):
    """Last phase done → no crash, no advance."""
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    board = await make_board(name="Last Board", slug="last-board")
    project = await _create_project(session, board.id)

    # Only one phase
    phase1 = await _create_phase(session, board.id, project.id, "Einzige Phase", sort_order=1, status="in_progress")
    await _create_subtask(session, board.id, phase1.id, "Sub 1", status="done", project_id=project.id)

    monitor = TaskMonitorMixin()
    with _patch_redis():
        with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
            await monitor._check_phase_completions(session)

    # Phase 1 goes to review (phase review)
    await session.refresh(phase1)
    assert phase1.status == "review"


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_auto_advance_skips_done_phases(mock_emit, session, make_board):
    """Skip phases that are already done, take the next inbox one."""
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    board = await make_board(name="Skip Board", slug="skip-board")
    project = await _create_project(session, board.id)

    phase1 = await _create_phase(session, board.id, project.id, "Phase 1", sort_order=1, status="in_progress")
    await _create_subtask(session, board.id, phase1.id, "Sub 1", status="done", project_id=project.id)

    # Phase 2 already done
    await _create_phase(session, board.id, project.id, "Phase 2", sort_order=2, status="done")

    # Phase 3 inbox → should be started
    phase3 = await _create_phase(session, board.id, project.id, "Phase 3", sort_order=3, status="inbox")

    monitor = TaskMonitorMixin()
    with _patch_redis():
        with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
            await monitor._check_phase_completions(session)

    # Phase 1 goes to review (phase review)
    await session.refresh(phase1)
    assert phase1.status == "review"

    # Phase 3 stays inbox (auto-advance only after phase 1 done)
    await session.refresh(phase3)
    assert phase3.status == "inbox"


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_auto_advance_without_project(mock_emit, session, make_board):
    """Standalone parent without project_id → no auto-advance (no crash)."""
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    board = await make_board(name="No Proj Board", slug="no-proj-board")

    # Parent without project_id
    from app.models.task import Task
    parent = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Standalone Parent",
        status="in_progress",
        sort_order=1,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    await _create_subtask(session, board.id, parent.id, "Sub", status="done")

    monitor = TaskMonitorMixin()
    with _patch_redis():
        with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock):
            await monitor._check_phase_completions(session)

    # Standalone parent goes to review (phase review)
    await session.refresh(parent)
    assert parent.status == "review"


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_auto_advance_dispatches_subtasks(mock_emit, session, make_board, make_agent):
    """Auto-advance dispatches inbox subtasks of the new phase."""
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    board = await make_board(name="Dispatch Board", slug="dispatch-board", auto_dispatch_enabled=True)
    project = await _create_project(session, board.id)
    agent = await make_agent(name="Cody", board_id=board.id)

    phase1 = await _create_phase(session, board.id, project.id, "Phase 1", sort_order=1, status="in_progress")
    await _create_subtask(session, board.id, phase1.id, "Sub 1.1", status="done", project_id=project.id)

    phase2 = await _create_phase(session, board.id, project.id, "Phase 2", sort_order=2, status="inbox")
    sub2 = await _create_subtask(
        session, board.id, phase2.id, "Sub 2.1", status="inbox",
        project_id=project.id, assigned_agent_id=agent.id,
    )

    # auto_dispatch_task and dependencies_met are lazily imported
    with patch("app.services.dispatch.auto_dispatch_task", new_callable=AsyncMock) as mock_dispatch, \
         patch("app.services.dispatch.dependencies_met", new_callable=AsyncMock, return_value=True), \
         patch("app.services.watchdog.core._create_background_task") as mock_bg, \
         patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock), \
         _patch_redis():

        monitor = TaskMonitorMixin()
        await monitor._check_phase_completions(session)

        # Phase 1 goes to review (auto-advance only after done)
        await session.refresh(phase1)
        assert phase1.status == "review"

        # Phase 2 stays inbox
        await session.refresh(phase2)
        assert phase2.status == "inbox"


@pytest.mark.asyncio
@patch("app.services.watchdog.task_monitor.emit_event", new_callable=AsyncMock)
async def test_phase_completion_dedup(mock_emit, session, make_board):
    """Phase completion fires only once (Redis dedup)."""
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    board = await make_board(name="Dedup Board", slug="dedup-board")
    project = await _create_project(session, board.id)

    phase1 = await _create_phase(session, board.id, project.id, "Phase 1", sort_order=1, status="in_progress")
    await _create_subtask(session, board.id, phase1.id, "Sub 1", status="done", project_id=project.id)

    monitor = TaskMonitorMixin()
    with _patch_redis():
        # First call → fires
        await monitor._check_phase_completions(session)
        call_count_1 = mock_emit.call_count

        # Second call → dedup prevents firing again
        await monitor._check_phase_completions(session)
        call_count_2 = mock_emit.call_count

    assert call_count_1 > 0
    assert call_count_2 == call_count_1  # No new event
