"""provision_agent_background — no silent fail on file sync errors.

OSS fresh-install path (agent deploy verification, host registry wave 1+2):
right after template instantiate, ~/.mc/agents/{slug}/claude-config/ doesn't
exist yet — sync_docker_agent_files then returns {"_error": ...}. Previously,
provision_agent_background marked the agent as 'provisioned' anyway
(ProvisionBadge "Live" without files/container). Now: status stays 'local' +
agent.provision_failed warning event with an actionable instruction.
"""
from __future__ import annotations

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from tests.conftest import test_engine


@pytest.fixture
def _patched_engine(monkeypatch):
    """provision_agent_background builds its own session from app.database.engine."""
    monkeypatch.setattr("app.database.engine", test_engine)


async def _make_cli_agent(name: str = "Fresh Agent") -> Agent:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            name=name,
            agent_runtime="cli-bridge",
            provision_status="local",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent


async def _reload(agent_id) -> Agent:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Agent, agent_id)


@pytest.mark.asyncio
async def test_sync_error_keeps_status_local(monkeypatch, _patched_engine):
    """_error in the sync result → provision_status stays 'local' + warning event."""
    from app.services import provisioning

    agent = await _make_cli_agent()

    async def fake_write_compose(session):
        return {"changed": "false"}

    async def fake_sync(session, ag):
        return {"_error": "claude-config dir not found: /tmp/nope"}

    events: list[tuple] = []

    async def fake_emit(session, event_type, message, **kwargs):
        events.append((event_type, message, kwargs.get("severity")))

    monkeypatch.setattr(
        "app.services.compose_renderer.write_compose_agents", fake_write_compose
    )
    monkeypatch.setattr(
        "app.services.docker_agent_sync.sync_docker_agent_files", fake_sync
    )
    monkeypatch.setattr("app.services.provisioning.emit_event", fake_emit)

    await provisioning.provision_agent_background(agent.id)

    reloaded = await _reload(agent.id)
    assert reloaded.provision_status == "local", (
        "Sync-Fehler darf den Agent nicht als 'provisioned' markieren"
    )
    assert len(events) == 1
    event_type, message, severity = events[0]
    assert event_type == "agent.provision_failed"
    assert severity == "warning"
    assert "Provision" in message  # Actionable instruction instead of silent fail


@pytest.mark.asyncio
async def test_sync_ok_marks_provisioned(monkeypatch, _patched_engine):
    """Regression guard: successful sync still marks 'provisioned'."""
    from app.services import provisioning

    agent = await _make_cli_agent(name="Synced Agent")

    async def fake_write_compose(session):
        return {"changed": "true"}

    async def fake_sync(session, ag):
        return {"SOUL.md": "written", "TOOLS.md": "written (from DB)"}

    async def fake_emit(session, event_type, message, **kwargs):
        pass

    # Provision autostart (test_provision_autostart.py) would otherwise really
    # call docker here — the success case is enough for this guard.
    monkeypatch.setattr(
        "app.services.docker_agent_sync.ensure_agent_container_started",
        lambda ag: {"status": "recreated", "container": "x", "mode": "recreate"},
    )

    monkeypatch.setattr(
        "app.services.compose_renderer.write_compose_agents", fake_write_compose
    )
    monkeypatch.setattr(
        "app.services.docker_agent_sync.sync_docker_agent_files", fake_sync
    )
    monkeypatch.setattr("app.services.provisioning.emit_event", fake_emit)

    await provisioning.provision_agent_background(agent.id)

    reloaded = await _reload(agent.id)
    assert reloaded.provision_status == "provisioned"
    assert reloaded.provisioned_at is not None
