import json
import tempfile
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def registry_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("MC_MCP_REGISTRY_DIR", tmp)
        yield Path(tmp)


@pytest.mark.asyncio
async def test_list_mcp_servers(auth_client, registry_dir):
    fs = registry_dir / "filesystem"
    fs.mkdir()
    (fs / "manifest.json").write_text(json.dumps({
        "name": "filesystem", "transport": "stdio",
        "command": "node", "args": [],
        "description": "files",
    }))

    resp = await auth_client.get("/api/v1/mcp-servers")
    assert resp.status_code == 200
    servers = resp.json()
    assert len(servers) == 1
    assert servers[0]["name"] == "filesystem"
    assert servers[0]["transport"] == "stdio"


@pytest.mark.asyncio
async def test_get_mcp_server_detail(auth_client, registry_dir):
    fs = registry_dir / "filesystem"
    fs.mkdir()
    (fs / "manifest.json").write_text(json.dumps({
        "name": "filesystem", "transport": "stdio",
        "command": "node", "args": ["/mc-servers/filesystem/index.js"],
    }))

    resp = await auth_client.get("/api/v1/mcp-servers/filesystem")
    assert resp.status_code == 200
    assert resp.json()["command"] == "node"


@pytest.mark.asyncio
async def test_get_mcp_server_not_found(auth_client, registry_dir):
    resp = await auth_client.get("/api/v1/mcp-servers/doesnt-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_agent_mcp_servers(auth_client, async_session, registry_dir):
    from app.models.agent import Agent
    from app.models.board import Board

    board = Board(name="MC Dev", slug="mc-dev")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    agent = Agent(name="Spark", role="R", scopes=[], board_id=board.id)
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}/mcp-servers",
        json={"mcp_servers": ["filesystem"]},
    )
    assert resp.status_code == 200
    await async_session.refresh(agent)
    assert agent.mcp_servers == ["filesystem"]


@pytest.mark.asyncio
async def test_post_mcp_server_happy_path(auth_client, registry_dir):
    resp = await auth_client.post("/api/v1/mcp-servers", json={
        "name": "test-server",
        "transport": "stdio",
        "command": "node",
        "args": ["/mc-servers/test-server/index.js"],
        "description": "A test server",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test-server"
    assert data["transport"] == "stdio"
    assert "installed_at" in data

    # Verify it appears in GET
    list_resp = await auth_client.get("/api/v1/mcp-servers")
    assert list_resp.status_code == 200
    names = [s["name"] for s in list_resp.json()]
    assert "test-server" in names


@pytest.mark.asyncio
async def test_post_mcp_server_duplicate(auth_client, registry_dir):
    payload = {"name": "dup-server", "transport": "http", "url": "https://example.com/mcp"}
    resp1 = await auth_client.post("/api/v1/mcp-servers", json=payload)
    assert resp1.status_code == 201
    resp2 = await auth_client.post("/api/v1/mcp-servers", json=payload)
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_post_mcp_server_missing_transport(auth_client, registry_dir):
    resp = await auth_client.post("/api/v1/mcp-servers", json={"name": "no-transport"})
    assert resp.status_code == 422  # Pydantic validation error


@pytest.mark.asyncio
async def test_delete_mcp_server_cleans_agents(auth_client, async_session, registry_dir):
    from app.models.agent import Agent
    from app.models.board import Board

    # Create a registry entry
    srv_dir = registry_dir / "cleanup-server"
    srv_dir.mkdir()
    (srv_dir / "manifest.json").write_text(json.dumps({
        "name": "cleanup-server", "transport": "stdio",
        "command": "node", "args": [],
    }))

    # Create an agent with this server assigned
    board = Board(name="Test Board", slug="test-board-del")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    agent = Agent(name="CleanupAgent", role="R", scopes=[], board_id=board.id,
                  mcp_servers=["cleanup-server"])
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    resp = await auth_client.delete("/api/v1/mcp-servers/cleanup-server")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "CleanupAgent" in data["cleaned_agents"]

    # Server no longer in registry
    get_resp = await auth_client.get("/api/v1/mcp-servers/cleanup-server")
    assert get_resp.status_code == 404

    # Agent assignment cleaned
    await async_session.refresh(agent)
    assert agent.mcp_servers is not None
    assert "cleanup-server" not in agent.mcp_servers


@pytest.mark.asyncio
async def test_delete_mcp_server_not_found(auth_client, registry_dir):
    resp = await auth_client.delete("/api/v1/mcp-servers/nonexistent-server")
    assert resp.status_code == 404
