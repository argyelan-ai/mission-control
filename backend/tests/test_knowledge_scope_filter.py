"""Phase 5 — MSY-05 backend scope-filter tests (Plan 05-01).

Verifies that ``GET /api/v1/knowledge?scope=global|board|agent`` filters
the result set to entries whose scope columns satisfy the requested
predicate:

- ``scope=global`` → ``board_id IS NULL AND agent_id IS NULL``
- ``scope=board&board_id=X`` → ``board_id = X``
- ``scope=agent&agent_id=Y`` → ``agent_id = Y``

The dropdown UI exists in ``MemoryPage.tsx`` but the request was silently
mapped to ``{}`` before this plan landed; backend support is what makes
the wiring observable.
"""
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.memory import BoardMemory
from tests.conftest import test_engine


async def _seed_three_scopes() -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one global + one board-scoped + one agent-scoped BoardMemory.
    Returns (board_id, agent_id)."""
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id,
            name="ScopeTest",
            slug=f"scope-{board_id.hex[:8]}",
            require_review_before_done=False,
        ))
        await s.commit()
        s.add(Agent(
            id=agent_id,
            board_id=board_id,
            name="ScopeAgent",
            role="researcher",
            scopes=["knowledge:read"],
            provision_status="provisioned",
        ))
        await s.commit()
        s.add(BoardMemory(
            content="global memory body",
            title="g",
            source="user",
            memory_type="knowledge",
        ))
        s.add(BoardMemory(
            content="board memory body",
            title="b",
            source="user",
            memory_type="knowledge",
            board_id=board_id,
        ))
        s.add(BoardMemory(
            content="agent memory body",
            title="a",
            source="user",
            memory_type="lesson",
            agent_id=agent_id,
        ))
        await s.commit()
    return board_id, agent_id


@pytest.mark.asyncio
async def test_scope_global_filters_to_nulls(auth_client):
    """MSY-05 D-23: ``GET /api/v1/knowledge?scope=global`` returns only
    entries where ``board_id IS NULL AND agent_id IS NULL``."""
    await _seed_three_scopes()
    resp = await auth_client.get("/api/v1/knowledge?scope=global")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    titles = [r["title"] for r in rows]
    assert "g" in titles, f"global entry must be present, got {titles}"
    assert "b" not in titles, f"board entry must be excluded, got {titles}"
    assert "a" not in titles, f"agent entry must be excluded, got {titles}"


@pytest.mark.asyncio
async def test_scope_board_filters_to_board(auth_client):
    """MSY-05 D-23: ``GET /api/v1/knowledge?scope=board&board_id=X``
    returns only entries where ``board_id = X``."""
    board_id, _ = await _seed_three_scopes()
    resp = await auth_client.get(f"/api/v1/knowledge?scope=board&board_id={board_id}")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    titles = [r["title"] for r in rows]
    assert "b" in titles, f"board entry must be present, got {titles}"
    assert "g" not in titles, f"global entry must be excluded, got {titles}"
    assert "a" not in titles, f"agent entry must be excluded, got {titles}"
