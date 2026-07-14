import uuid
import pytest
from unittest.mock import patch

from app.models.agent import Agent
from app.services import agent_lifecycle
from app.services import compose_renderer


@pytest.mark.asyncio
async def test_archive_busy_agent_raises_and_does_not_stop(session):
    agent = Agent(name="Busy", slug="busy", agent_runtime="cli-bridge",
                  current_task_id=uuid.uuid4())
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.docker_agent_sync, "stop_docker_agent_container") as stop:
        with pytest.raises(agent_lifecycle.AgentBusyError):
            await agent_lifecycle.archive_agent(session, agent)
    stop.assert_not_called()
    await session.refresh(agent)
    assert agent.archived_at is None


@pytest.mark.asyncio
async def test_archive_free_clibridge_agent_stops_container_and_sets_flag(session):
    agent = Agent(name="Dev", slug="dev", agent_runtime="cli-bridge")
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.docker_agent_sync, "stop_docker_agent_container",
                      return_value={"ok": "true"}) as stop, \
         patch.object(compose_renderer, "prune_compose_agent",
                      return_value={"changed": "true"}) as prune:
        await agent_lifecycle.archive_agent(session, agent)
    stop.assert_called_once()
    prune.assert_called_once_with("dev")
    await session.refresh(agent)
    assert agent.archived_at is not None


@pytest.mark.asyncio
async def test_archive_host_agent_does_not_prune_compose(session):
    """Host agents are not compose-managed — the prune call must be cli-bridge-only."""
    agent = Agent(name="HostDev2", slug="hostdev2", agent_runtime="host")
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.agent_bootstrap, "_run_launchctl_bootout",
                      return_value={"unloaded": True}), \
         patch.object(compose_renderer, "prune_compose_agent") as prune:
        await agent_lifecycle.archive_agent(session, agent)
    prune.assert_not_called()
    await session.refresh(agent)
    assert agent.archived_at is not None


@pytest.mark.asyncio
async def test_archive_host_agent_boots_out_label(session):
    agent = Agent(name="HostDev", slug="hostdev", agent_runtime="host")
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.agent_bootstrap, "_run_launchctl_bootout",
                      return_value={"unloaded": True}) as bootout:
        await agent_lifecycle.archive_agent(session, agent)
    bootout.assert_called_once_with("com.mc.agent.hostdev")
    await session.refresh(agent)
    assert agent.archived_at is not None


@pytest.mark.asyncio
async def test_archive_is_idempotent(session):
    from app.utils import utcnow
    agent = Agent(name="Already", slug="already", agent_runtime="manual", archived_at=utcnow())
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.docker_agent_sync, "stop_docker_agent_container") as stop:
        result = await agent_lifecycle.archive_agent(session, agent)
    stop.assert_not_called()
    assert result.archived_at is not None


@pytest.mark.asyncio
async def test_archive_stop_failure_still_sets_flag(session):
    agent = Agent(name="Hung", slug="hung", agent_runtime="cli-bridge")
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.docker_agent_sync, "stop_docker_agent_container",
                      side_effect=RuntimeError("docker daemon down")):
        await agent_lifecycle.archive_agent(session, agent)  # must not raise
    await session.refresh(agent)
    assert agent.archived_at is not None


@pytest.mark.asyncio
async def test_restore_clears_flag_and_starts_container(session):
    from app.utils import utcnow
    agent = Agent(name="Dev", slug="dev", agent_runtime="cli-bridge", archived_at=utcnow())
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.docker_agent_sync, "ensure_agent_container_started",
                      return_value={"ok": "true"}) as start, \
         patch.object(compose_renderer, "write_compose_agents",
                      return_value={"changed": "true"}) as write:
        await agent_lifecycle.restore_agent(session, agent)
    write.assert_called_once_with(session)
    start.assert_called_once()
    await session.refresh(agent)
    assert agent.archived_at is None


# ── Singleton-bridge guard ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_archive_singleton_host_bridge_raises(session):
    agent = Agent(name="Hermes", slug="hermes", agent_runtime="host")
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.agent_bootstrap, "_run_launchctl_bootout") as bootout:
        with pytest.raises(agent_lifecycle.SingletonAgentError):
            await agent_lifecycle.archive_agent(session, agent)
    bootout.assert_not_called()
    await session.refresh(agent)
    assert agent.archived_at is None


@pytest.mark.asyncio
async def test_archive_host_agent_that_merely_uses_hermes_harness_is_not_singleton(session):
    """A throwaway generic agent with slug='dev' is NOT a singleton bridge even
    if it happens to run harness=hermes — only slug in {boss,hermes,grok} +
    agent_runtime=='host' counts."""
    agent = Agent(name="Dev", slug="dev", agent_runtime="host", harness="hermes")
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.agent_bootstrap, "_run_launchctl_bootout",
                      return_value={"unloaded": True}) as bootout:
        await agent_lifecycle.archive_agent(session, agent)
    bootout.assert_called_once()
    await session.refresh(agent)
    assert agent.archived_at is not None


@pytest.mark.asyncio
async def test_restore_singleton_host_bridge_raises(session):
    from app.utils import utcnow
    agent = Agent(name="Grok", slug="grok", agent_runtime="host", archived_at=utcnow())
    session.add(agent); await session.commit()
    with patch.object(agent_lifecycle.agent_bootstrap, "_run_launchctl_bootstrap") as bootstrap:
        with pytest.raises(agent_lifecycle.SingletonAgentError):
            await agent_lifecycle.restore_agent(session, agent)
    bootstrap.assert_not_called()
    await session.refresh(agent)
    assert agent.archived_at is not None
