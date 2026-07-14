import pytest
from unittest.mock import patch

from app.utils import utcnow


@pytest.mark.asyncio
async def test_delete_active_agent_returns_409(auth_client, make_agent, session):
    from app.models.agent import Agent
    agent = await make_agent(name="Active", agent_runtime="cli-bridge")
    resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 409
    # agent still exists — gate blocked the delete
    assert await session.get(Agent, agent.id) is not None


@pytest.mark.asyncio
async def test_delete_archived_clibridge_agent_removes_container(auth_client, make_agent, session):
    from app.models.agent import Agent
    agent = await make_agent(name="ArchivedDev", agent_runtime="cli-bridge", archived_at=utcnow())
    with patch("app.services.docker_agent_sync.remove_docker_agent_container",
               return_value={"ok": "true"}) as rm:
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 204
    rm.assert_called_once()
    assert await session.get(Agent, agent.id) is None


@pytest.mark.asyncio
async def test_delete_archived_agent_purges_redis(auth_client, make_agent, fake_redis):
    agent = await make_agent(name="RedisDev", agent_runtime="cli-bridge", archived_at=utcnow())
    await fake_redis.set(f"mc:agent:{agent.id}:dispatch_lock", "1")
    with patch("app.services.docker_agent_sync.remove_docker_agent_container", return_value={"ok": "true"}):
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 204
    assert await fake_redis.get(f"mc:agent:{agent.id}:dispatch_lock") is None


@pytest.mark.asyncio
async def test_delete_archived_host_agent_runs_bootout(auth_client, make_agent, session):
    """Archived host-runtime agent delete: launchd bootout runs before rmtree, best-effort."""
    from app.models.agent import Agent
    agent = await make_agent(name="ArchivedHost", agent_runtime="host", archived_at=utcnow())
    with patch("app.services.agent_bootstrap._run_launchctl_bootout", return_value={"ok": "true"}) as bootout, \
         patch("app.services.host_provisioning.teardown_host_agent_files") as teardown:
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 204
    bootout.assert_called_once()
    teardown.assert_called_once()
    assert await session.get(Agent, agent.id) is None
