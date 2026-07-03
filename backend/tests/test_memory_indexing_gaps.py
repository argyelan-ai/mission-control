"""Tests for the 2026-04-15 memory-indexing-gaps fix.

Verifies that all BoardMemory write paths call `index_memory()` so new
entries automatically land in the Qdrant vector layer.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task  # ← add
from app.models.board import Board
from app.models.memory import BoardMemory
from tests.conftest import test_engine


async def _make_researcher(name: str = "TestResearcher") -> tuple[Agent, str]:
    """Researcher agent with knowledge:write scope + token."""
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    bid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=bid, name=f"Board-{name}", slug=f"research-{bid.hex[:8]}",
            require_review_before_done=False,
        )
        s.add(board)
        await s.commit()

        agent = Agent(
            id=uuid.uuid4(),
            board_id=bid,
            name=name,
            role="researcher",
            scopes=["knowledge:read", "knowledge:write", "memory:write"],
            agent_token_hash=token_hash,
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent, raw_token


@pytest.mark.asyncio
async def test_agent_create_knowledge_triggers_index_memory(client):
    """POST /agent/knowledge must call index_memory() (semantic layer)."""
    agent, token = await _make_researcher()

    with patch(
        "app.services.memory_indexing.index_memory",
        new=AsyncMock(return_value="semantic"),
    ) as mock_index:
        resp = await client.post(
            "/api/v1/agent/knowledge",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "Recherche-Ergebnis zu Scroll-Animationen",
                "title": "Scroll-Anim Research",
                "memory_type": "research",
                "scope": "agent",
            },
        )

    assert resp.status_code == 201, resp.text
    mock_index.assert_awaited_once()
    indexed_entry = mock_index.await_args.args[0]
    assert isinstance(indexed_entry, BoardMemory)
    assert indexed_entry.memory_type == "research"
    assert indexed_entry.title == "Scroll-Anim Research"


@pytest.mark.asyncio
async def test_save_research_triggers_index_memory(auth_client):
    """POST /research/{id}/save must call index_memory() (semantic layer).

    The research UI saves results via user auth → save_research() in
    routers/research.py. Lands as BoardMemory(memory_type="research") and
    must be indexed in the memory_semantic Qdrant layer.
    """
    from app.models.board import PlannerMessage, Project

    bid = uuid.uuid4()
    pid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=bid,
            name="ResearchBoard",
            slug=f"research-board-{bid.hex[:8]}",
            require_review_before_done=False,
        )
        s.add(board)
        project = Project(
            id=pid,
            board_id=bid,
            name="Scroll-Anim Recherche",
            project_type="research",
            status="planning",
            created_by="research",
        )
        s.add(project)
        reply = PlannerMessage(
            project_id=pid,
            role="assistant",
            content="## Zusammenfassung\nScroll-Animationen sind top.",
        )
        s.add(reply)
        await s.commit()

    with patch(
        "app.services.memory_indexing.index_memory",
        new=AsyncMock(return_value="semantic"),
    ) as mock_index:
        resp = await auth_client.post(
            f"/api/v1/research/{pid}/save",
            json={"title": "Scroll-Anim Research", "tags": ["ui"]},
        )

    assert resp.status_code == 200, resp.text
    mock_index.assert_awaited_once()
    indexed = mock_index.await_args.args[0]
    assert isinstance(indexed, BoardMemory)
    assert indexed.memory_type == "research"
    assert indexed.source == "research"


@pytest.mark.asyncio
async def test_auto_memory_task_completion_writes_task_comment(fake_redis):
    """W4.2: record_task_completion writes a TaskComment instead of BoardMemory.

    After the W4 redirect, the task_done summary lands as
    TaskComment(comment_type='reflection', author_type='system') — no longer
    as BoardMemory. index_memory() is NOT called for this path (no vault
    noise from telemetry).
    """
    from app.models.task import TaskComment
    from app.services.auto_memory import record_task_completion
    from sqlmodel import select

    bid = uuid.uuid4()
    aid = uuid.uuid4()
    tid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=bid, name="AMBoard",
            slug=f"am-board-{bid.hex[:8]}",
        ))
        s.add(Agent(
            id=aid, board_id=bid, name="AMCody",
            role="developer",
        ))
        s.add(Task(
            id=tid, board_id=bid, title="Smoketask",
            status="done", priority="medium",
            assigned_agent_id=aid,
        ))
        await s.commit()

    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)), \
         patch(
            "app.services.memory_indexing.index_memory",
            new=AsyncMock(return_value="episodic"),
         ) as mock_index:
        await record_task_completion(tid, aid)

    # W4.2: task_done is now a TaskComment, not a BoardMemory
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(TaskComment)
            .where(TaskComment.task_id == tid)
            .where(TaskComment.comment_type == "reflection")
            .where(TaskComment.author_type == "system")
        )
        comments = list(result.all())
    assert len(comments) == 1, f"Expected 1 system reflection comment, got {len(comments)}"
    assert "Smoketask" in comments[0].content

    # index_memory is NOT called for the task_done path (no vault indexing for telemetry)
    mock_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_memory_task_failure_is_agent_scoped(fake_redis):
    """record_task_failure must write a lesson with agent_id (agent layer).

    Without agent_id, layer_for() would return None and index_memory()
    would be a no-op. The fix sets agent_id=agent_id in the BoardMemory
    constructor.
    """
    from app.services.auto_memory import record_task_failure

    bid = uuid.uuid4()
    aid = uuid.uuid4()
    tid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=bid, name="AMFailBoard",
            slug=f"am-fail-board-{bid.hex[:8]}",
        ))
        s.add(Agent(
            id=aid, board_id=bid, name="FailCody",
            role="developer",
        ))
        s.add(Task(
            id=tid, board_id=bid, title="Failtask",
            status="failed", priority="medium",
            assigned_agent_id=aid,
        ))
        await s.commit()

    with patch("app.services.auto_memory.engine", test_engine), \
         patch("app.services.auto_memory.get_redis", AsyncMock(return_value=fake_redis)), \
         patch(
            "app.services.memory_indexing.index_memory",
            new=AsyncMock(return_value="agent"),
         ) as mock_index:
        await record_task_failure(tid, aid)

    mock_index.assert_awaited_once()
    indexed = mock_index.await_args.args[0]
    assert isinstance(indexed, BoardMemory)
    assert indexed.memory_type == "lesson"
    assert indexed.agent_id == aid

