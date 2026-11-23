"""_trigger_sync_config: per-runtime apply registry, honest result, and NO
automatic container restart.

Before: every install restarted the agent's docker container, which killed
every running chat turn of that agent. Host and openclaw agents got only a
log line while the install reported success.
"""
from unittest.mock import AsyncMock, patch

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
    monkeypatch.setattr("app.database.engine", test_engine)


async def _agent(session: AsyncSession, **kw) -> Agent:
    agent = Agent(role="Reviewer", scopes=[], **kw)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _sync_patches():
    return (
        patch("app.services.docker_agent_sync.sync_docker_agent_files",
              new_callable=AsyncMock),
        patch("app.services.docker_agent_sync.restart_docker_agent_container"),
        patch("app.services.mcp_sync.sync_agent_mcp_to_disk"),
    )


@pytest.mark.asyncio
async def test_docker_agent_files_synced_but_container_never_restarted(
    async_session: AsyncSession,
):
    agent = await _agent(async_session, name="Rex", agent_runtime="cli-bridge",
                         harness="claude")
    from app.services.install_executor import _trigger_sync_config

    p_sync, p_restart, p_mcp = _sync_patches()
    with p_sync as mock_sync, p_restart as mock_restart, p_mcp as mock_mcp:
        report = await _trigger_sync_config(agent.id, "skill")

    mock_sync.assert_awaited_once()
    mock_mcp.assert_called_once()
    mock_restart.assert_not_called()
    assert report["status"] == "needs_restart"
    assert report["note"] == "takes effect at next chat start"
    assert report["runtime"] == "cli-bridge"
    assert report["harness"] == "claude"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["omp", "kimi"])
async def test_harness_that_ignores_skill_files_reports_not_applied(
    async_session: AsyncSession, harness: str,
):
    """Capability matrix says omp/kimi ignore MC skill files -> never claim
    the skill will work there."""
    agent = await _agent(async_session, name=f"H-{harness}",
                         agent_runtime="cli-bridge", harness=harness)
    from app.services.install_executor import _trigger_sync_config

    p_sync, p_restart, p_mcp = _sync_patches()
    with p_sync, p_restart as mock_restart, p_mcp:
        report = await _trigger_sync_config(agent.id, "skill")

    mock_restart.assert_not_called()
    assert report["status"] == "not_applied"
    assert harness in report["note"]


@pytest.mark.asyncio
async def test_host_agent_reports_not_applied_and_writes_nothing(
    async_session: AsyncSession,
):
    agent = await _agent(async_session, name="Lead", agent_runtime="host")
    from app.services.install_executor import _trigger_sync_config

    p_sync, p_restart, p_mcp = _sync_patches()
    with p_sync as mock_sync, p_restart as mock_restart, p_mcp as mock_mcp:
        report = await _trigger_sync_config(agent.id, "skill")

    mock_sync.assert_not_awaited()
    mock_restart.assert_not_called()
    mock_mcp.assert_not_called()
    assert report["status"] == "not_applied"
    assert report["runtime"] == "host"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["openclaw", "manual", "claude-code"])
async def test_runtime_without_install_path_reports_not_applied(
    async_session: AsyncSession, runtime: str,
):
    """No special openclaw branch any more (gateway retired, ADR-039): any
    runtime without a registry entry is reported honestly."""
    agent = await _agent(async_session, name=f"R-{runtime}", agent_runtime=runtime)
    from app.services.install_executor import _trigger_sync_config

    p_sync, p_restart, p_mcp = _sync_patches()
    with p_sync as mock_sync, p_restart as mock_restart, p_mcp:
        report = await _trigger_sync_config(agent.id, "plugin")

    mock_sync.assert_not_awaited()
    mock_restart.assert_not_called()
    assert report["status"] == "not_applied"
    assert runtime in report["note"]


def test_registry_has_no_openclaw_entry():
    from app.services.install_executor import RUNTIME_APPLIERS

    assert set(RUNTIME_APPLIERS) == {"cli-bridge", "host"}


def test_executor_source_never_restarts_a_container():
    """Structural guard: the restart helper is not referenced at all."""
    import inspect

    import app.services.install_executor as mod

    assert "restart_docker_agent_container" not in inspect.getsource(mod)
