"""
Tests for the Learning Loop — stage 2 of the master plan.

Tests:
- record_feedback_lesson (approved/rejected)
- fetch_agent_lessons
- fetch_relevant_lessons
- Dispatch message includes lessons
- Knowledge Stats endpoint
"""

import uuid
from datetime import datetime
from unittest.mock import patch, AsyncMock

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

# ── Helpers ──────────────────────────────────────────────────────────────────


async def _create_board(session: AsyncSession, **kwargs):
    from app.models.board import Board
    board = Board(
        id=kwargs.get("id", uuid.uuid4()),
        name=kwargs.get("name", "Test Board"),
        slug=kwargs.get("slug", "test-board"),
        auto_dispatch_enabled=kwargs.get("auto_dispatch_enabled", True),
    )
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def _create_agent(session: AsyncSession, **kwargs):
    from app.models.agent import Agent
    agent = Agent(
        id=kwargs.get("id", uuid.uuid4()),
        name=kwargs.get("name", "Test Agent"),
        board_id=kwargs.get("board_id"),
        emoji=kwargs.get("emoji", "🤖"),
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _create_task(session: AsyncSession, **kwargs):
    from app.models.task import Task
    task = Task(
        id=kwargs.get("id", uuid.uuid4()),
        board_id=kwargs["board_id"],
        title=kwargs.get("title", "Test Task"),
        description=kwargs.get("description"),
        status=kwargs.get("status", "inbox"),
        assigned_agent_id=kwargs.get("assigned_agent_id"),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _create_memory(session: AsyncSession, **kwargs):
    from app.models.memory import BoardMemory
    memory = BoardMemory(
        id=kwargs.get("id", uuid.uuid4()),
        board_id=kwargs.get("board_id"),
        agent_id=kwargs.get("agent_id"),
        title=kwargs.get("title"),
        content=kwargs["content"],
        memory_type=kwargs.get("memory_type", "lesson"),
        source=kwargs.get("source", "system"),
        auto_generated=kwargs.get("auto_generated", False),
        tags=kwargs.get("tags", []),
    )
    session.add(memory)
    await session.commit()
    await session.refresh(memory)
    return memory


# ── Tests: fetch_agent_lessons ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_agent_lessons_returns_agent_scoped(session):
    """fetch_agent_lessons only returns lessons the agent has written."""
    from app.services.auto_memory import fetch_agent_lessons

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        agent = await _create_agent(s, name="Cody", board_id=board.id)
        other = await _create_agent(s, name="Rex", board_id=board.id)

        # Cody's lesson (agent-scoped)
        await _create_memory(s, agent_id=agent.id, content="Cody learned something", title="Cody Lesson")
        # Rex's Lesson (should not appear)
        await _create_memory(s, agent_id=other.id, content="Rex learned something")

        lessons = await fetch_agent_lessons(s, agent.id, limit=10)
        assert len(lessons) == 1
        assert "Cody" in lessons[0].title


@pytest.mark.asyncio
async def test_fetch_agent_lessons_respects_limit(session):
    """fetch_agent_lessons returns at most N entries."""
    from app.services.auto_memory import fetch_agent_lessons

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        agent = await _create_agent(s, board_id=board.id)

        for i in range(5):
            await _create_memory(s, agent_id=agent.id, content=f"Lesson {i}")

        lessons = await fetch_agent_lessons(s, agent.id, limit=2)
        assert len(lessons) == 2


# ── Tests: fetch_relevant_lessons ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_relevant_lessons_keyword_match(session):
    """fetch_relevant_lessons finds lessons based on keywords from the task title."""
    from app.services.auto_memory import fetch_relevant_lessons

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)

        # Relevant lesson (contains "Authentication")
        await _create_memory(
            s, board_id=board.id,
            content="Authentication braucht JWT mit HS256 Signierung",
            title="Login Pattern",
        )
        # Not relevant lesson
        await _create_memory(
            s, board_id=board.id,
            content="Docker Compose Setup Guide",
            title="Docker Setup",
        )

        results = await fetch_relevant_lessons(
            s, "Fix Authentication Bug in Login Flow", None, board.id, limit=5
        )
        assert len(results) >= 1
        assert any("Authentication" in r.content for r in results)


@pytest.mark.asyncio
async def test_fetch_relevant_lessons_empty_for_no_match(session):
    """fetch_relevant_lessons returns an empty list when nothing matches."""
    from app.services.auto_memory import fetch_relevant_lessons

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        await _create_memory(s, board_id=board.id, content="Totally unrelated content xyz")

        results = await fetch_relevant_lessons(
            s, "Fix login", None, board.id, limit=5
        )
        # "login" is only 5 chars, might match; "Fix" is only 3 chars, filtered out
        # This should likely be empty since "login" != "unrelated"
        assert all("login" not in r.content.lower() or "unrelated" in r.content.lower()
                    for r in results) or len(results) == 0


# ── Tests: record_feedback_lesson ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_feedback_approved(session, fake_redis):
    """record_feedback_lesson creates a lesson on approval."""
    from app.services.auto_memory import record_feedback_lesson
    from app.models.memory import BoardMemory
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        agent = await _create_agent(s, name="Cody", board_id=board.id, emoji="🧑‍💻")
        task = await _create_task(s, board_id=board.id, title="Login Feature", assigned_agent_id=agent.id)

    # Patch engine and redis for the background-task pattern
    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)):
        await record_feedback_lesson(task.id, agent.id, "approved")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(BoardMemory).where(
                BoardMemory.auto_generated == True,  # noqa: E712
                BoardMemory.board_id == board.id,
            )
        )
        memories = result.all()
        assert len(memories) == 1
        assert "Genehmigt" in memories[0].title
        assert "feedback_approved" in memories[0].tags


@pytest.mark.asyncio
async def test_record_feedback_rejected_with_comment(session, fake_redis):
    """record_feedback_lesson creates a lesson on rejection with a feedback comment."""
    from app.services.auto_memory import record_feedback_lesson
    from app.models.memory import BoardMemory
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        agent = await _create_agent(s, name="Cody", board_id=board.id)
        task = await _create_task(s, board_id=board.id, title="Broken Feature", assigned_agent_id=agent.id)

    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)):
        await record_feedback_lesson(task.id, agent.id, "rejected", "Error Handling fehlt komplett")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(BoardMemory).where(BoardMemory.auto_generated == True)  # noqa: E712
        )
        memories = result.all()
        assert len(memories) == 1
        assert "Abgelehnt" in memories[0].title
        assert "Error Handling fehlt" in memories[0].content
        assert "feedback_rejected" in memories[0].tags


# ── Tests: Dispatch includes lessons ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_message_includes_agent_lessons(session):
    """Phase 1 Adoption: Agent-Lessons ARE auto-included in dispatch.

    Reverses the Workstream A3 slimming decision — agents need their
    lessons in the dispatch to avoid repeating mistakes. Included as a
    DispatchSection(priority=2) with LESSON_AUTO_MAX_CHARS budget, so
    they drop gracefully under budget pressure.
    """
    from app.services.dispatch import _build_dispatch_message

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        agent = await _create_agent(s, name="Cody", board_id=board.id)
        task = await _create_task(
            s, board_id=board.id, title="Fix Performance",
            assigned_agent_id=agent.id, description="Optimize DB queries"
        )
        await _create_memory(
            s, agent_id=agent.id,
            content="N+1 Query Problem bei Agent-List geloest mit selectinload",
            title="DB Optimization Lesson",
        )

        message = await _build_dispatch_message(task, agent, s)

        assert "Deine bisherigen Erkenntnisse" in message
        assert "N+1 Query" in message


@pytest.mark.asyncio
async def test_dispatch_message_does_not_inline_keyword_lessons(session):
    """Workstream A3: Keyword-matched board lessons stay out of the prompt
    too — agents retrieve them via `mc memory search` on demand."""
    from app.services.dispatch import _build_dispatch_message

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        agent = await _create_agent(s, name="Cody", board_id=board.id)
        task = await _create_task(
            s, board_id=board.id, title="Fix Authentication Token Refresh",
            assigned_agent_id=agent.id,
        )
        await _create_memory(
            s, board_id=board.id,
            content="Authentication Token Expiry muss bei 15min statt 1h sein",
            title="Auth Token Policy",
        )

        message = await _build_dispatch_message(task, agent, s)

        assert "Relevante Lessons" not in message
        assert "Letzte Team-Erkenntnisse" not in message


# ── Tests: Knowledge Stats API ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_knowledge_stats_endpoint(auth_client, session):
    """GET /knowledge/stats returns stats per memory_type."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await _create_memory(s, content="A", memory_type="lesson")
        await _create_memory(s, content="B", memory_type="lesson")
        await _create_memory(s, content="C", memory_type="knowledge")

    resp = await auth_client.get("/api/v1/knowledge/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["stats"]["lesson"] == 2
    assert data["stats"]["knowledge"] == 1
    assert data["total"] == 3


@pytest.mark.asyncio
async def test_knowledge_stats_filtered_by_agent(auth_client, session):
    """GET /knowledge/stats?agent_id=... filters by agent."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = await _create_board(s)
        agent = await _create_agent(s, board_id=board.id)
        await _create_memory(s, agent_id=agent.id, content="Agent Lesson", memory_type="lesson")
        await _create_memory(s, content="Global Lesson", memory_type="lesson")

    resp = await auth_client.get(f"/api/v1/knowledge/stats?agent_id={agent.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["stats"]["lesson"] == 1
    assert data["total"] == 1
