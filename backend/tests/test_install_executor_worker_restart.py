"""Verify _trigger_sync_config triggers container restart for cli-bridge agents
so claude-code picks up new .mcp.json / settings.json after install/uninstall.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent


@pytest.fixture(autouse=True)
def _fake_redis_for_install(fake_redis, monkeypatch):
    """Route install-executor's Redis calls to fakeredis + point the internal
    AsyncSession at the test engine so the function can load Agent rows."""
    from tests.conftest import test_engine

    async def _get_fake():
        return fake_redis

    monkeypatch.setattr("app.redis_client.get_redis", _get_fake)
    # _trigger_sync_config imports engine from app.database
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.mark.asyncio
async def test_trigger_sync_config_restarts_cli_bridge_container(
    async_session: AsyncSession,
):
    """After sync for a cli-bridge agent, the container must be restarted."""
    agent = Agent(
        name="Rex", role="Reviewer", scopes=[],
        cli_skills=[], cli_plugins=[], mcp_servers=[],
        agent_runtime="cli-bridge",
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.install_executor import _trigger_sync_config

    with patch("app.services.docker_agent_sync.sync_docker_agent_files",
               new_callable=AsyncMock) as mock_sync, \
         patch("app.services.docker_agent_sync.restart_docker_agent_container") as mock_restart, \
         patch("app.services.mcp_sync.sync_agent_mcp_to_disk") as mock_mcp_sync:
        mock_restart.return_value = {"status": "restarted", "container": "mc-agent-rex"}

        await _trigger_sync_config(agent.id)

        mock_sync.assert_awaited_once()
        mock_mcp_sync.assert_called_once()
        mock_restart.assert_called_once()
        # The agent passed to restart should have matching id
        called_agent = mock_restart.call_args[0][0]
        assert called_agent.id == agent.id


@pytest.mark.asyncio
async def test_trigger_sync_config_restart_failure_is_non_fatal(
    async_session: AsyncSession,
):
    """If container restart fails, the install should not error — files are
    already on disk and will be picked up on next organic restart."""
    agent = Agent(
        name="Rex", role="Reviewer", scopes=[],
        agent_runtime="cli-bridge",
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.install_executor import _trigger_sync_config

    with patch("app.services.docker_agent_sync.sync_docker_agent_files",
               new_callable=AsyncMock), \
         patch("app.services.docker_agent_sync.restart_docker_agent_container",
               side_effect=RuntimeError("docker daemon unavailable")), \
         patch("app.services.mcp_sync.sync_agent_mcp_to_disk"):
        # Must not raise — restart failure is logged but non-fatal
        await _trigger_sync_config(agent.id)


@pytest.mark.asyncio
async def test_trigger_sync_config_host_agent_skips_all_sync(
    async_session: AsyncSession,
):
    """Host agents (Boss) — DB state updated but file sync skipped entirely.
    Boss's claude-config dir name ('boss-host') doesn't match derived slug
    ('boss'), so writing would land in wrong directory. Operator regenerates
    manually and runs launchctl kickstart."""
    agent = Agent(
        name="Boss", role="orchestrator", scopes=[],
        agent_runtime="host",
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.install_executor import _trigger_sync_config

    with patch("app.services.docker_agent_sync.sync_docker_agent_files",
               new_callable=AsyncMock) as mock_sync, \
         patch("app.services.docker_agent_sync.restart_docker_agent_container") as mock_restart, \
         patch("app.services.mcp_sync.sync_agent_mcp_to_disk") as mock_mcp_sync:
        await _trigger_sync_config(agent.id)

        # Host agents: no file writes, no docker calls — operator does it
        mock_sync.assert_not_awaited()
        mock_restart.assert_not_called()
        mock_mcp_sync.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_sync_config_openclaw_skips_everything(
    async_session: AsyncSession,
):
    """openclaw agents (Henry) manage themselves via Gateway — no action here."""
    agent = Agent(
        name="Henry", role="Lead", scopes=[],
        agent_runtime="openclaw",
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    from app.services.install_executor import _trigger_sync_config

    with patch("app.services.docker_agent_sync.sync_docker_agent_files",
               new_callable=AsyncMock) as mock_sync, \
         patch("app.services.docker_agent_sync.restart_docker_agent_container") as mock_restart, \
         patch("app.services.mcp_sync.sync_agent_mcp_to_disk") as mock_mcp_sync:
        await _trigger_sync_config(agent.id)

        mock_sync.assert_not_awaited()
        mock_restart.assert_not_called()
        mock_mcp_sync.assert_not_called()
