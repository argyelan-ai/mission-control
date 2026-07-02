"""Phase 2 — sync_host_agent_files for Boss + Hermes (runtime=host).

Boss + Hermes don't go through the docker-compose openclaude stack, so the
existing ``sync_docker_agent_files`` early-returns for them. The new
``sync_host_agent_files`` writes the same SOUL/HEARTBEAT/USER/MEMORY/TOOLS
template→DB→file flow to ``agent.workspace_path/claude-config/`` instead.

The dispatcher ``sync_agent_files`` picks the right writer based on
``agent.agent_runtime`` so callers don't sprinkle the host check everywhere.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.services.docker_agent_sync import (
    sync_agent_files,
    sync_docker_agent_files,
    sync_host_agent_files,
)
from tests.conftest import test_engine


def _make_host_agent(workspace_path: Path) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        name="HostTestAgent",
        role="orchestrator",
        emoji="🧪",
        is_board_lead=True,
        scopes=["vault:read", "vault:write", "chat:write", "heartbeat"],
        agent_runtime="host",
        workspace_path=str(workspace_path),
        tools_md="# TOOLS\n\nfake tools content",
    )


@pytest.mark.asyncio
async def test_sync_host_writes_soul_to_workspace(tmp_path: Path):
    workspace = tmp_path / "boss"
    workspace.mkdir()

    agent = _make_host_agent(workspace)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_host_agent_files(s, agent)

    soul_path = workspace / "claude-config" / "SOUL.md"
    tools_path = workspace / "claude-config" / "TOOLS.md"
    assert soul_path.exists(), "SOUL.md must land in workspace_path/claude-config/"
    assert tools_path.exists(), "TOOLS.md must land too"
    assert tools_path.read_text() == "# TOOLS\n\nfake tools content"
    assert results.get("SOUL.md") == "written"
    assert results.get("TOOLS.md") == "written (from DB)"


@pytest.mark.asyncio
async def test_sync_host_creates_claude_config_dir_if_missing(tmp_path: Path):
    workspace = tmp_path / "boss-no-dir"
    workspace.mkdir()  # workspace exists but no claude-config/ yet

    agent = _make_host_agent(workspace)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        await sync_host_agent_files(s, agent)

    assert (workspace / "claude-config").is_dir()
    assert (workspace / "claude-config" / "SOUL.md").exists()


@pytest.mark.asyncio
async def test_sync_host_rejects_non_host_runtime(tmp_path: Path):
    agent = Agent(
        id=uuid.uuid4(),
        name="DockerAgent",
        role="developer",
        emoji="🤖",
        agent_runtime="cli-bridge",
        workspace_path=str(tmp_path / "docker"),
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_host_agent_files(s, agent)
    assert "_error" in results
    assert "non-host" in results["_error"]


@pytest.mark.asyncio
async def test_sync_host_returns_error_when_workspace_missing(tmp_path: Path):
    agent = Agent(
        id=uuid.uuid4(),
        name="NoWorkspace",
        role="orchestrator",
        emoji="❓",
        agent_runtime="host",
        workspace_path=None,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_host_agent_files(s, agent)
    assert "_error" in results
    assert "workspace_path" in results["_error"]


@pytest.mark.asyncio
async def test_dispatcher_routes_host_to_host_sync(tmp_path: Path):
    """sync_agent_files must pick the host writer when runtime=host."""
    workspace = tmp_path / "boss-disp"
    workspace.mkdir()
    agent = _make_host_agent(workspace)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_agent_files(s, agent)

    # Host sync writes to workspace_path, NOT to ~/.openclaw/agents/...
    assert (workspace / "claude-config" / "SOUL.md").exists()
    assert results.get("SOUL.md") == "written"


@pytest.mark.asyncio
async def test_dispatcher_skips_docker_for_host_agent(tmp_path: Path):
    """If a caller accidentally pipes a host agent to sync_docker_agent_files,
    it returns the documented skip-marker. Dispatcher avoids the silent skip."""
    workspace = tmp_path / "boss-direct-docker"
    workspace.mkdir()
    agent = _make_host_agent(workspace)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        results = await sync_docker_agent_files(s, agent)
    assert results == {"_skipped": "host runtime"}
