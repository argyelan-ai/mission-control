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


@pytest.mark.asyncio
async def test_delete_singleton_host_bridge_returns_422_even_if_archived(auth_client, make_agent, session):
    """boss/hermes/grok are managed via launchd/Runtime Cockpit — delete must
    refuse with a clear 422 rather than the generic archived-required 409,
    even when archived_at happens to be set."""
    from app.models.agent import Agent
    agent = await make_agent(name="Boss", slug="boss", agent_runtime="host", archived_at=utcnow())
    resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 422
    assert await session.get(Agent, agent.id) is not None


@pytest.mark.asyncio
async def test_delete_archived_generic_dev_agent_still_204(auth_client, make_agent, session):
    """Regression: a generic archived agent slug='dev' (not a singleton bridge)
    must keep deleting fine — the singleton guard must not over-match."""
    from app.models.agent import Agent
    agent = await make_agent(name="Dev", slug="dev", agent_runtime="host", archived_at=utcnow())
    with patch("app.services.agent_bootstrap._run_launchctl_bootout", return_value={"ok": "true"}), \
         patch("app.services.host_provisioning.teardown_host_agent_files"):
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 204
    assert await session.get(Agent, agent.id) is None


@pytest.mark.asyncio
async def test_delete_archived_agent_purges_metrics_cache_and_rate_limit(auth_client, make_agent, fake_redis):
    from app.redis_client import RedisKeys
    agent = await make_agent(name="RedisDev2", agent_runtime="cli-bridge", archived_at=utcnow())
    await fake_redis.set(RedisKeys.agent_metrics_cache(str(agent.id)), "cached")
    await fake_redis.set(RedisKeys.agent_rate_limit(str(agent.id)), "1")
    with patch("app.services.docker_agent_sync.remove_docker_agent_container", return_value={"ok": "true"}):
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 204
    assert await fake_redis.get(RedisKeys.agent_metrics_cache(str(agent.id))) is None
    assert await fake_redis.get(RedisKeys.agent_rate_limit(str(agent.id))) is None
