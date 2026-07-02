"""
Tests for POST /api/v1/agent/install-requests

Covers:
- Happy path: creates pending Approval (201)
- Allowlist rejection: source not in allowlist (400)
- Already-installed conflict (409)
- Idempotency: duplicate request returns same approval_id (200)
- Scope enforcement: agents:manage required for other-agent targets (403)
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helper ────────────────────────────────────────────────────────────────


async def _make_agent(
    *,
    name: str,
    scopes: list[str],
    board_id: uuid.UUID,
    cli_skills: list | None = None,
    cli_plugins: list | None = None,
):
    """Create agent with PBKDF2 token, return (agent, raw_token)."""
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            name=name,
            role="lead" if "agents:manage" in scopes else "developer",
            scopes=scopes,
            board_id=board_id,
            agent_token_hash=token_hash,
            cli_skills=cli_skills,
            cli_plugins=cli_plugins,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

    return agent, raw_token


async def _make_board(name: str = "MC Dev", slug: str = "mc-dev") -> uuid.UUID:
    """Create a board and return its id."""
    from app.models.board import Board

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name=name, slug=slug)
        s.add(board)
        await s.commit()
        await s.refresh(board)
        return board.id


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_request_happy_path(client: AsyncClient, fake_redis):
    board_id = await _make_board()
    boss, token = await _make_agent(
        name="Boss Host", scopes=["agents:manage"], board_id=board_id
    )
    target, _ = await _make_agent(
        name="Spark", scopes=[], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "skill",
            "operation": "install",
            "source": "github:anthropic/skill-web-performance",
            "name": "web-performance",
            "target_agent_id": str(target.id),
            "reason": "Agent failed 3 debug tasks",
            "autonomy_level": "L2",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["existing"] is False
    uuid.UUID(body["approval_id"])  # must be valid UUID


@pytest.mark.asyncio
async def test_install_request_rejects_unknown_source(client: AsyncClient, fake_redis):
    board_id = await _make_board(slug="mc-dev-2")
    boss, token = await _make_agent(
        name="Boss Host 2", scopes=["agents:manage"], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "skill",
            "operation": "install",
            "source": "github:evil-user/malware",
            "name": "evil",
            "target_agent_id": str(boss.id),
            "reason": "test attack attempt",
        },
    )
    assert resp.status_code == 400
    assert "allowlist" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_install_request_rejects_already_installed(client: AsyncClient, fake_redis):
    board_id = await _make_board(slug="mc-dev-3")
    boss, token = await _make_agent(
        name="Boss Host 3", scopes=["agents:manage"], board_id=board_id
    )
    target, _ = await _make_agent(
        name="Spark 3",
        scopes=[],
        board_id=board_id,
        cli_skills=["web-performance"],
    )

    resp = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "skill",
            "operation": "install",
            "source": "github:anthropic/skill-web-performance",
            "name": "web-performance",
            "target_agent_id": str(target.id),
            "reason": "testing already installed guard",
        },
    )
    assert resp.status_code == 409
    assert "already" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_install_request_is_idempotent(client: AsyncClient, fake_redis):
    board_id = await _make_board(slug="mc-dev-4")
    boss, token = await _make_agent(
        name="Boss Host 4", scopes=["agents:manage"], board_id=board_id
    )
    target, _ = await _make_agent(
        name="Spark 4", scopes=[], board_id=board_id
    )

    body = {
        "type": "skill",
        "operation": "install",
        "source": "github:anthropic/skill-web-performance",
        "name": "web-performance",
        "target_agent_id": str(target.id),
        "reason": "Agent needs it",
    }

    resp1 = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    resp2 = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )

    assert resp1.status_code == 201, resp1.text
    assert resp2.status_code == 200, resp2.text
    assert resp1.json()["approval_id"] == resp2.json()["approval_id"]
    assert resp2.json()["existing"] is True


@pytest.mark.asyncio
async def test_install_request_requires_agents_manage_for_other_target(
    client: AsyncClient, fake_redis
):
    board_id = await _make_board(slug="mc-dev-5")
    # Agent with explicit limited scopes (no agents:manage)
    requester, token = await _make_agent(
        name="Cody", scopes=["tasks:read", "tasks:write"], board_id=board_id
    )
    other, _ = await _make_agent(
        name="Rex", scopes=[], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "skill",
            "operation": "install",
            "source": "github:anthropic/skill-web-performance",
            "name": "web-performance",
            "target_agent_id": str(other.id),
            "reason": "test scope enforcement",
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_install_request_empty_scopes_treated_as_all(
    client: AsyncClient, fake_redis
):
    """Agents with scopes=[] have ALL_SCOPES per backward-compat — should allow
    cross-agent install-requests like Boss."""
    board_id = await _make_board(slug="mc-dev-6")
    requester, token = await _make_agent(
        name="boss-like", scopes=[], board_id=board_id
    )
    target, _ = await _make_agent(
        name="spark", scopes=[], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "skill",
            "operation": "install",
            "source": "github:anthropic/skill-web-performance",
            "name": "web-performance",
            "target_agent_id": str(target.id),
            "reason": "Legacy agent with empty scopes should be allowed",
        },
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_install_request_mcp_happy_path(client: AsyncClient, fake_redis):
    """MCP install-requests should be accepted (Phase 2)."""
    board_id = await _make_board(slug="mc-dev-7")
    boss, token = await _make_agent(
        name="Boss Host MCP", scopes=["agents:manage"], board_id=board_id
    )
    target, _ = await _make_agent(
        name="Spark MCP", scopes=[], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/install-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "mcp",
            "operation": "install",
            "source": "npm:@modelcontextprotocol/server-filesystem",
            "name": "filesystem",
            "target_agent_id": str(target.id),
            "reason": "Agent braucht Filesystem-Zugriff für Task-Workspace",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["existing"] is False
    uuid.UUID(body["approval_id"])  # must be valid UUID
