"""Tests for GET /api/v1/agent/me — self-lookup for agents.

Convenience endpoint that replaces trial-and-error with GET /agent/agents/{id}
(404) or /agent/me variants. Live learning from 2026-04-24: Boss spent
~6min searching for the right endpoints before `mc delegate` worked.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent(
    *,
    name: str = "TestAgent",
    is_board_lead: bool = False,
    scopes: list[str] | None = None,
    cli_skills: list[str] | None = None,
    cli_plugins: list[str] | None = None,
    current_task_id: uuid.UUID | None = None,
):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    raw_token, token_hash = generate_agent_token()
    board_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name=f"Board-{name}", slug=f"b-{name.lower()}"))
        await s.commit()

        agent = Agent(
            id=uuid.uuid4(),
            name=name,
            role="developer",
            is_board_lead=is_board_lead,
            board_id=board_id,
            agent_runtime="cli-bridge",
            agent_token_hash=token_hash,
            scopes=scopes if scopes is not None else ["heartbeat", "tasks:read"],
            cli_skills=cli_skills,
            cli_plugins=cli_plugins,
            current_task_id=current_task_id,
            model="glm-5.1:cloud",
            provision_status="provisioned",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

    return agent, raw_token, board_id


@pytest.mark.asyncio
async def test_me_returns_agent_profile(client: AsyncClient):
    """Happy path: agent calls /me, gets all fields back."""
    agent, token, board_id = await _make_agent(
        name="AlphaTest",
        scopes=["heartbeat", "tasks:read", "tasks:write"],
        cli_skills=["summarize", "medewo-gruppe-brand"],
        cli_plugins=None,  # null = all installed
    )

    resp = await client.get(
        "/api/v1/agent/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["id"] == str(agent.id)
    assert data["name"] == "AlphaTest"
    assert data["is_board_lead"] is False
    assert data["board_id"] == str(board_id)
    assert data["agent_runtime"] == "cli-bridge"
    assert "heartbeat" in data["scopes"]
    assert data["cli_skills"] == ["summarize", "medewo-gruppe-brand"]
    assert data["cli_plugins"] is None  # None = all
    assert data["current_task"] is None
    assert data["provision_status"] == "provisioned"


@pytest.mark.asyncio
async def test_me_works_with_minimal_scope(client: AsyncClient):
    """No special scope required — heartbeat alone is enough."""
    _, token, _ = await _make_agent(
        name="MinScope",
        scopes=["heartbeat"],  # only heartbeat, no tasks:read
    )

    resp = await client.get(
        "/api/v1/agent/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_me_includes_current_task(client: AsyncClient):
    """When agent.current_task_id is set, the task summary is included."""
    from app.models.task import Task

    # Create the agent first, then the task, then set agent.current_task_id
    agent, token, board_id = await _make_agent(name="BetaTest")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = Task(
            board_id=board_id,
            title="Laufender Task",
            description="Test",
            status="in_progress",
            assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)

        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
        fresh.current_task_id = task.id
        s.add(fresh)
        await s.commit()
        task_id = task.id

    resp = await client.get(
        "/api/v1/agent/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_task"] is not None
    assert data["current_task"]["id"] == str(task_id)
    assert data["current_task"]["title"] == "Laufender Task"
    assert data["current_task"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_me_board_lead_flag(client: AsyncClient):
    """is_board_lead is returned correctly."""
    _, token, _ = await _make_agent(
        name="BossTest", is_board_lead=True,
        scopes=["heartbeat", "tasks:create", "agents:manage"],
    )

    resp = await client.get(
        "/api/v1/agent/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["is_board_lead"] is True


@pytest.mark.asyncio
async def test_me_requires_auth(client: AsyncClient):
    """Without an auth header → 401."""
    resp = await client.get("/api/v1/agent/me")
    assert resp.status_code in (401, 403)
