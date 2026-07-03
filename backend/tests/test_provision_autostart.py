"""provision_agent_background — container autostart after successful provision.

One-click deploy (OSS core feature): provision renders files+compose AND brings
the container up. Previously, provision ended in 'provisioned' without a running
container — it only started on runtime switch or manually via start-all.sh.

Protection rule: if the container is already running (re-provision of an active
agent), it is NOT recreated — an agent mid-task must not get shot down.
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


@pytest.fixture
def _happy_sync(monkeypatch):
    """Compose render + file sync succeed, events collected."""
    events: list[tuple] = []

    async def fake_write_compose(session):
        return {"changed": "true"}

    async def fake_sync(session, ag):
        return {"SOUL.md": "written", "TOOLS.md": "written (from DB)"}

    async def fake_emit(session, event_type, message, **kwargs):
        events.append((event_type, message, kwargs.get("severity")))

    monkeypatch.setattr(
        "app.services.compose_renderer.write_compose_agents", fake_write_compose
    )
    monkeypatch.setattr(
        "app.services.docker_agent_sync.sync_docker_agent_files", fake_sync
    )
    monkeypatch.setattr("app.services.provisioning.emit_event", fake_emit)
    return events


async def _make_agent(name: str = "Auto Agent", runtime: str = "cli-bridge") -> Agent:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(name=name, agent_runtime=runtime, provision_status="local")
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent


async def _reload(agent_id) -> Agent:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Agent, agent_id)


@pytest.mark.asyncio
async def test_provision_starts_container(monkeypatch, _patched_engine, _happy_sync):
    """Successful provision calls ensure_agent_container_started and marks provisioned."""
    from app.services import provisioning

    agent = await _make_agent()
    calls: list = []

    def fake_ensure(ag):
        calls.append(ag.id)
        return {"status": "recreated", "container": "mc-agent-auto-agent", "mode": "recreate"}

    monkeypatch.setattr(
        "app.services.docker_agent_sync.ensure_agent_container_started", fake_ensure
    )

    await provisioning.provision_agent_background(agent.id)

    assert calls == [agent.id], "Container-Autostart muss genau einmal laufen"
    reloaded = await _reload(agent.id)
    assert reloaded.provision_status == "provisioned"
    assert reloaded.provisioned_at is not None
    event_type, message, severity = _happy_sync[0]
    assert event_type == "agent.provisioned"
    assert "recreated" in message  # Container status visible in the event


@pytest.mark.asyncio
async def test_running_container_not_recreated(monkeypatch, _patched_engine, _happy_sync):
    """already-running counts as success — re-provision doesn't bounce an active agent."""
    from app.services import provisioning

    agent = await _make_agent(name="Busy Agent")

    def fake_ensure(ag):
        return {"status": "already-running", "container": "mc-agent-busy-agent", "mode": "none"}

    monkeypatch.setattr(
        "app.services.docker_agent_sync.ensure_agent_container_started", fake_ensure
    )

    await provisioning.provision_agent_background(agent.id)

    reloaded = await _reload(agent.id)
    assert reloaded.provision_status == "provisioned"


@pytest.mark.asyncio
async def test_container_start_error_marks_error(monkeypatch, _patched_engine, _happy_sync):
    """Container start error → provision_status 'error' + warning event (no silent fail)."""
    from app.services import provisioning

    agent = await _make_agent(name="Broken Agent")

    def fake_ensure(ag):
        return {"status": "error: no such image", "container": "mc-agent-broken-agent", "mode": "recreate"}

    monkeypatch.setattr(
        "app.services.docker_agent_sync.ensure_agent_container_started", fake_ensure
    )

    await provisioning.provision_agent_background(agent.id)

    reloaded = await _reload(agent.id)
    assert reloaded.provision_status == "error", (
        "Files ok aber kein Container → ehrlicher error-Status statt 'provisioned'"
    )
    event_type, message, severity = _happy_sync[0]
    assert event_type == "agent.provision_failed"
    assert severity == "warning"
    assert "docker logs" in message  # Actionable instruction


@pytest.mark.asyncio
async def test_sync_error_skips_autostart(monkeypatch, _patched_engine):
    """File sync error → autostart must not even be attempted."""
    from app.services import provisioning

    agent = await _make_agent(name="No Files Agent")
    calls: list = []

    async def fake_write_compose(session):
        return {"changed": "false"}

    async def fake_sync(session, ag):
        return {"_error": "claude-config dir not found"}

    async def fake_emit(session, event_type, message, **kwargs):
        pass

    def fake_ensure(ag):
        calls.append(ag.id)
        return {"status": "recreated", "container": "x", "mode": "recreate"}

    monkeypatch.setattr(
        "app.services.compose_renderer.write_compose_agents", fake_write_compose
    )
    monkeypatch.setattr(
        "app.services.docker_agent_sync.sync_docker_agent_files", fake_sync
    )
    monkeypatch.setattr("app.services.provisioning.emit_event", fake_emit)
    monkeypatch.setattr(
        "app.services.docker_agent_sync.ensure_agent_container_started", fake_ensure
    )

    await provisioning.provision_agent_background(agent.id)

    assert calls == [], "Ohne Files kein Container-Start"
    reloaded = await _reload(agent.id)
    assert reloaded.provision_status == "local"


@pytest.mark.asyncio
async def test_host_agent_no_autostart(monkeypatch, _patched_engine):
    """Host agents (launchd) have no Docker container — no autostart call."""
    from app.services import provisioning

    agent = await _make_agent(name="Boss Agent", runtime="host")
    calls: list = []

    def fake_ensure(ag):
        calls.append(ag.id)
        return {"status": "recreated", "container": "x", "mode": "recreate"}

    async def fake_emit(session, event_type, message, **kwargs):
        pass

    monkeypatch.setattr(
        "app.services.docker_agent_sync.ensure_agent_container_started", fake_ensure
    )
    monkeypatch.setattr("app.services.provisioning.emit_event", fake_emit)

    await provisioning.provision_agent_background(agent.id)

    assert calls == []
    reloaded = await _reload(agent.id)
    assert reloaded.provision_status == "provisioned"


def test_ensure_skips_running_container(monkeypatch):
    """ensure_agent_container_started: running container → no force_recreate."""
    from app.services import docker_agent_sync as das

    agent = Agent(name="Live Agent", agent_runtime="cli-bridge")
    recreate_calls: list = []

    monkeypatch.setattr(das, "_agent_container_running", lambda name: True)
    monkeypatch.setattr(
        das, "restart_docker_agent_container",
        lambda ag, **kw: recreate_calls.append(kw) or {"status": "recreated"},
    )

    result = das.ensure_agent_container_started(agent)

    assert result["status"] == "already-running"
    assert recreate_calls == []


def test_ensure_recreates_stopped_container(monkeypatch):
    """ensure_agent_container_started: no/stopped container → force_recreate=True."""
    from app.services import docker_agent_sync as das

    agent = Agent(name="Cold Agent", agent_runtime="cli-bridge")
    recreate_calls: list = []

    def fake_restart(ag, **kw):
        recreate_calls.append(kw)
        return {"status": "recreated", "container": "mc-agent-cold-agent", "mode": "recreate"}

    monkeypatch.setattr(das, "_agent_container_running", lambda name: None)
    monkeypatch.setattr(das, "restart_docker_agent_container", fake_restart)

    result = das.ensure_agent_container_started(agent)

    assert result["status"] == "recreated"
    assert recreate_calls == [{"force_recreate": True}]
