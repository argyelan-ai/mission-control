"""Tests for Phase 2A: Observability & Hygiene.

- Evidence guard: at least 1 progress/resolution comment before review
- Dispatch-decision logging: find_dispatch_target returns a reason
- Owner callback: owner_agent_id is used for the callback
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

_BROADCAST_PATCH = patch("app.services.activity.broadcast", new_callable=AsyncMock)

REFLECTION_TEXT = (
    "## Was gemacht\nFeature implementiert.\n"
    "## Was funktioniert\nTests gruen.\n"
    "## Was unklar\nNichts.\n"
    "## Lesson\nImmer Tests zuerst schreiben."
)


async def _add_reflection(task_id: uuid.UUID, agent_id: uuid.UUID):
    """Helper: create a reflection comment (satisfies Rule 4)."""
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        c = TaskComment(
            task_id=task_id,
            author_type="agent",
            author_agent_id=agent_id,
            comment_type="reflection",
            content=REFLECTION_TEXT,
        )
        s.add(c)
        await s.commit()


async def _setup_phase2a_scenario():
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    dev_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(
            id=board_id, name="Phase2A Board", slug=f"p2a-{uuid.uuid4().hex[:8]}",
            require_review_before_done=True,
        )
        s.add(board)

        lead_token, lead_hash = generate_agent_token()
        lead = Agent(
            id=lead_id, name="Henry", role="lead",
            board_id=board_id, agent_token_hash=lead_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:create", "tasks:manage"],
            agent_runtime="cli-bridge",  # Phase 30: dispatch target requires poll-runtime
        )
        s.add(lead)

        dev_token, dev_hash = generate_agent_token()
        dev = Agent(
            id=dev_id, name="Sparky", role="developer",
            board_id=board_id, agent_token_hash=dev_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            agent_runtime="cli-bridge",  # Phase 30: dispatch target requires poll-runtime
        )
        s.add(dev)
        await s.commit()

    return {
        "board_id": board_id,
        "lead_id": lead_id, "lead_token": lead_token,
        "dev_id": dev_id, "dev_token": dev_token,
    }


# ────────────────────────────────────────────────────────────
# Evidence Guard Tests
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_blocked_without_evidence(client):
    """Agent cannot set review without at least 1 progress/resolution comment.

    Reflection comment has been mandatory since Phase E (Rule 4) and is satisfied here.
    After that, the evidence guard applies (no progress/resolution/checkpoint → 409).
    """
    ids = await _setup_phase2a_scenario()
    from app.models.task import Task

    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=task_id, board_id=ids["board_id"],
            title="No Evidence Task", status="in_progress",
            assigned_agent_id=ids["dev_id"],
        )
        s.add(task)
        await s.commit()

    # Post reflection (Rule 4 satisfied) — but no evidence comment
    await _add_reflection(task_id, ids["dev_id"])

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {ids['dev_token']}"},
            json={"status": "review"},
        )

    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    assert "Evidence erforderlich" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_review_allowed_with_evidence(client):
    """Agent can set review with evidence + reflection."""
    ids = await _setup_phase2a_scenario()
    from app.models.task import Task, TaskComment

    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=task_id, board_id=ids["board_id"],
            title="With Evidence Task", status="in_progress",
            assigned_agent_id=ids["dev_id"],
        )
        comment = TaskComment(
            task_id=task_id,
            author_type="agent", author_agent_id=ids["dev_id"],
            comment_type="progress",
            content="**Update** — Implementierung abgeschlossen",
        )
        s.add_all([task, comment])
        await s.commit()

    # Reflection as the last comment (Rule 4)
    await _add_reflection(task_id, ids["dev_id"])

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {ids['dev_token']}"},
            json={"status": "review"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_review_allowed_with_checkpoint(client):
    """Checkpoint comment also counts as evidence, reflection last."""
    ids = await _setup_phase2a_scenario()
    from app.models.task import Task, TaskComment

    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=task_id, board_id=ids["board_id"],
            title="Checkpoint Evidence", status="in_progress",
            assigned_agent_id=ids["dev_id"],
        )
        comment = TaskComment(
            task_id=task_id,
            author_type="agent", author_agent_id=ids["dev_id"],
            comment_type="checkpoint",
            content="- [x] Branch erstellt\n- [x] Tests geschrieben",
        )
        s.add_all([task, comment])
        await s.commit()

    # Reflection as the last comment (Rule 4)
    await _add_reflection(task_id, ids["dev_id"])

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{ids['board_id']}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {ids['dev_token']}"},
            json={"status": "review"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


# ────────────────────────────────────────────────────────────
# Dispatch Decision Logging Tests
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_target_returns_reason():
    """find_dispatch_target returns an (agent, reason) tuple."""
    from app.services.dispatch import find_dispatch_target
    from app.models.task import Task

    ids = await _setup_phase2a_scenario()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Dispatch Reason Test", status="inbox",
        )
        s.add(task)
        await s.commit()

        agent, reason = await find_dispatch_target(s, task, ids["board_id"])

    assert agent is not None
    assert agent.name == "Henry"  # Board lead has priority
    assert reason == "board_lead"


@pytest.mark.asyncio
async def test_dispatch_target_explicit_assignment():
    """Explicit assigned_agent_id takes precedence over board lead."""
    from app.services.dispatch import find_dispatch_target
    from app.models.task import Task

    ids = await _setup_phase2a_scenario()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            id=uuid.uuid4(), board_id=ids["board_id"],
            title="Explicit Assignment Test", status="inbox",
            assigned_agent_id=ids["dev_id"],  # explicitly Sparky (not board lead)
        )
        s.add(task)
        await s.commit()

        agent, reason = await find_dispatch_target(s, task, ids["board_id"])

    assert agent is not None
    assert agent.name == "Sparky"       # directly to the assigned agent
    assert reason == "explicit_assignment"


@pytest.mark.asyncio
async def test_dispatch_target_empty_board():
    """Empty board returns (None, 'no_agents_on_board')."""
    from app.services.dispatch import find_dispatch_target
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Empty", slug=f"empty-{uuid.uuid4().hex[:8]}")
        task = Task(id=uuid.uuid4(), board_id=board_id, title="Empty Board Test", status="inbox")
        s.add_all([board, task])
        await s.commit()

        agent, reason = await find_dispatch_target(s, task, board_id)

    assert agent is None
    assert reason == "no_agents_on_board"
