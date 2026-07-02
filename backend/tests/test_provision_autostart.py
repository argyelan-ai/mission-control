"""provision_agent_background — Container-Autostart nach erfolgreichem Provision.

One-Click-Deploy (OSS-Kern-Feature): Provision rendert Files+Compose UND bringt
den Container hoch. Vorher endete Provision in 'provisioned' ohne laufenden
Container — der startete erst beim Runtime-Switch oder manuell via start-all.sh.

Schutzregel: Läuft der Container bereits (Re-Provision eines aktiven Agents),
wird er NICHT recreated — ein Agent mitten im Task darf nicht abgeschossen werden.
"""
from __future__ import annotations

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from tests.conftest import test_engine


@pytest.fixture
def _patched_engine(monkeypatch):
    """provision_agent_background baut seine eigene Session aus app.database.engine."""
    monkeypatch.setattr("app.database.engine", test_engine)


@pytest.fixture
def _happy_sync(monkeypatch):
    """Compose-Render + File-Sync erfolgreich, Events gesammelt."""
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
    """Erfolgreicher Provision ruft ensure_agent_container_started und markiert provisioned."""
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
    assert "recreated" in message  # Container-Status im Event sichtbar


@pytest.mark.asyncio
async def test_running_container_not_recreated(monkeypatch, _patched_engine, _happy_sync):
    """already-running gilt als Erfolg — Re-Provision bounce't keinen aktiven Agent."""
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
    """Container-Start-Fehler → provision_status 'error' + Warning-Event (kein Silent-Fail)."""
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
    assert "docker logs" in message  # Handlungsanweisung


@pytest.mark.asyncio
async def test_sync_error_skips_autostart(monkeypatch, _patched_engine):
    """File-Sync-Fehler → Autostart darf gar nicht erst versucht werden."""
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
    """Host-Agents (launchd) haben keinen Docker-Container — kein Autostart-Aufruf."""
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
    """ensure_agent_container_started: laufender Container → kein force_recreate."""
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
    """ensure_agent_container_started: kein/gestoppter Container → force_recreate=True."""
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
