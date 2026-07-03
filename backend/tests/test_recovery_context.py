"""
Tests for build_recovery_context() — rich recovery context from task comments.

TDD: tests first, implementation after.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment


# ── Helper function: create comment ─────────────────────────────────


async def _create_comment(
    session: AsyncSession,
    task_id: uuid.UUID,
    comment_type: str = "progress",
    content: str = "Test comment",
    created_at: datetime | None = None,
) -> TaskComment:
    """Create a TaskComment in the DB and return it."""
    comment = TaskComment(
        id=uuid.uuid4(),
        task_id=task_id,
        author_type="agent",
        comment_type=comment_type,
        content=content,
        created_at=created_at or datetime.utcnow(),
    )
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return comment


# ── Helper function: board + task setup ──────────────────────────────────


async def _setup_board_and_task(
    session: AsyncSession,
    assigned_agent_id: uuid.UUID | None = None,
) -> Task:
    """Create board + task and return the task."""
    board = Board(id=uuid.uuid4(), name="Test Board", slug=f"test-{uuid.uuid4().hex[:8]}")
    session.add(board)
    await session.commit()

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Recovery Test Task",
        assigned_agent_id=assigned_agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_context_returns_none_without_comments(session: AsyncSession):
    """Without comments → return None."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    result = await build_recovery_context(session, task)
    assert result is None


@pytest.mark.asyncio
async def test_recovery_context_includes_progress_comments(session: AsyncSession):
    """Progress comments appear under 'Latest Progress'.

    Workstream A4: `checkpoint` comments no longer exist — migration 0082
    moved them into `progress`, and new code posts `progress` via
    `mc comment progress`.
    """
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    await _create_comment(session, task.id, "progress", "Schritt 1 erledigt", now - timedelta(minutes=30))
    await _create_comment(session, task.id, "progress", "Models erstellt", now - timedelta(minutes=20))
    await _create_comment(session, task.id, "progress", "Tests geschrieben", now - timedelta(minutes=10))

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Recovery" in result
    assert "Schritt 1 erledigt" in result
    assert "Models erstellt" in result
    assert "Tests geschrieben" in result
    assert "progress" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_blocker(session: AsyncSession):
    """Blocker comment is shown with the BLOCKER label."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "blocker", "Warte auf API-Key")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "BLOCKER" in result
    assert "Warte auf API-Key" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_feedback(session: AsyncSession):
    """Reviewer feedback is shown with the REVIEWER-FEEDBACK label."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "feedback", "Tests fehlen fuer Edge-Cases")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "REVIEWER-FEEDBACK" in result
    assert "Tests fehlen fuer Edge-Cases" in result


@pytest.mark.asyncio
async def test_recovery_context_limits_to_5_comments(session: AsyncSession):
    """10 comments → only the newest 5 appear (Workstream A4 cap)."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    for i in range(10):
        prefix = "OLD" if i < 5 else "NEW"
        await _create_comment(
            session,
            task.id,
            "progress",
            f"Fortschritt {prefix}-{i:02d}",
            now - timedelta(minutes=10 - i),
        )

    result = await build_recovery_context(session, task)

    assert result is not None
    # The oldest 5 (OLD-00 through OLD-04) should NOT be included
    for i in range(5):
        assert f"Fortschritt OLD-{i:02d}" not in result
    # The newest 5 (NEW-05 through NEW-09) should be included
    for i in range(5, 10):
        assert f"Fortschritt NEW-{i:02d}" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_workspace_info(session: AsyncSession):
    """Agent with workspace_path → path in the result."""
    from app.services.dispatch import build_recovery_context

    agent = Agent(
        id=uuid.uuid4(),
        name="Cody",
        workspace_path="/home/henry/.openclaw/workspace-cody",
    )
    session.add(agent)
    await session.commit()

    task = await _setup_board_and_task(session, assigned_agent_id=agent.id)
    await _create_comment(session, task.id, "progress", "Arbeite am Feature")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "/home/henry/.openclaw/workspace-cody" in result
    assert "Workspace" in result


@pytest.mark.asyncio
async def test_recovery_context_ignores_message_type(session: AsyncSession):
    """Comments of type 'message' are NOT included."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "message", "Hallo, wie geht's?")

    result = await build_recovery_context(session, task)

    # Only message comments → no recovery context
    assert result is None


@pytest.mark.asyncio
async def test_agent_dispatch_config_defaults(session: AsyncSession):
    """dispatch_config defaults to empty dict."""
    agent = Agent(name="TestAgent")
    session.add(agent)
    await session.flush()

    loaded = await session.get(Agent, agent.id)
    assert loaded.dispatch_config == {}


# ── Tests for _get_agent_timeout ─────────────────────────────────────


def test_get_agent_timeout_returns_default():
    """Without dispatch_config, the global default is returned."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="TestAgent")
    assert _get_agent_timeout(agent, "stale_progress_minutes", 30) == 30


def test_get_agent_timeout_returns_agent_value():
    """With dispatch_config, the agent value is returned."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="Cody", dispatch_config={"stale_progress_minutes": 45})
    assert _get_agent_timeout(agent, "stale_progress_minutes", 30) == 45


def test_get_agent_timeout_falls_back_on_missing_key():
    """Missing key in dispatch_config falls back to the default."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="Cody", dispatch_config={"stale_progress_minutes": 45})
    assert _get_agent_timeout(agent, "ack_timeout_minutes", 10) == 10
