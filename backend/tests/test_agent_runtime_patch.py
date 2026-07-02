"""Tests for agent runtime switching API (Phase 3 + Phase 15).

Phase 15 routed the runtime PATCH path through `agent_runtime_switch`.
Tests mock the four side-effects (sync_docker_agent_files,
restart_docker_agent_container, wait_for_agent_healthy, write_compose_agents)
on the switch-service namespace.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch

from app.models.agent import Agent
from app.models.runtime import Runtime


async def _make_runtime(session, *, slug="test-rt", enabled=True) -> Runtime:
    rt = Runtime(
        slug=slug,
        display_name=f"Test Runtime {slug}",
        runtime_type="lmstudio",
        endpoint="http://example.com/v1",
        model_identifier="test-model",
        enabled=enabled,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _make_agent(session, *, agent_runtime="cli-bridge"):
    agent = Agent(
        name=f"TestAgent-{uuid.uuid4().hex[:6]}",
        agent_runtime=agent_runtime,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_patch_agent_runtime_success(auth_client, async_session, fake_redis):
    rt = await _make_runtime(async_session)
    agent = await _make_agent(async_session, agent_runtime="cli-bridge")

    async def _fake_get_redis():
        return fake_redis

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container",
               side_effect=lambda *a, **k: {"status": "restarted", "container": "test", "mode": "restart"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy",
               AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents",
               AsyncMock(return_value={"changed": "false"})), \
         patch("app.services.agent_runtime_switch.get_redis", _fake_get_redis):
        resp = await auth_client.patch(
            f"/api/v1/agents/{agent.id}",
            json={"runtime_id": str(rt.id)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runtime_id"] == str(rt.id)
    assert "_switch" in body
    assert body["_switch"]["new_runtime"]["slug"] == rt.slug


@pytest.mark.asyncio
async def test_patch_agent_runtime_rejected_for_host(auth_client, async_session):
    rt = await _make_runtime(async_session)
    agent = await _make_agent(async_session, agent_runtime="host")

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}",
        json={"runtime_id": str(rt.id)},
    )
    assert resp.status_code == 422
    assert "cli-bridge" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_patch_agent_runtime_rejected_for_openclaw(auth_client, async_session):
    rt = await _make_runtime(async_session)
    agent = await _make_agent(async_session, agent_runtime="openclaw")

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}",
        json={"runtime_id": str(rt.id)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_agent_runtime_404_on_missing(auth_client, async_session):
    agent = await _make_agent(async_session, agent_runtime="cli-bridge")
    fake_id = str(uuid.uuid4())

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}",
        json={"runtime_id": fake_id},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_agent_runtime_disabled(auth_client, async_session):
    rt = await _make_runtime(async_session, slug="disabled", enabled=False)
    agent = await _make_agent(async_session, agent_runtime="cli-bridge")

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}",
        json={"runtime_id": str(rt.id)},
    )
    assert resp.status_code == 422
    assert "disabled" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_runtime_db(auth_client):
    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json={
            "slug": "new-rt",
            "display_name": "New",
            "runtime_type": "lmstudio",
            "endpoint": "http://x.example/v1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "new-rt"


@pytest.mark.asyncio
async def test_create_runtime_duplicate_slug(auth_client, async_session):
    await _make_runtime(async_session, slug="dup")
    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json={
            "slug": "dup",
            "display_name": "Dup",
            "runtime_type": "lmstudio",
            "endpoint": "http://x/v1",
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_runtime_db(auth_client, async_session):
    rt = await _make_runtime(async_session, slug="to-update")
    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}",
        json={"display_name": "Renamed", "enabled": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Renamed"
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_runtime_agents_badge_count(auth_client, async_session):
    rt = await _make_runtime(async_session, slug="with-agents")
    for _ in range(2):
        a = Agent(name=f"A{uuid.uuid4().hex[:6]}", agent_runtime="cli-bridge", runtime_id=rt.id)
        async_session.add(a)
    await async_session.commit()

    resp = await auth_client.get(f"/api/v1/runtimes/db/{rt.slug}/agents")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


@pytest.mark.asyncio
async def test_patch_runtime_id_explicit_null_clears_binding(auth_client, async_session):
    """PATCH {"runtime_id": null} must clear agent.runtime_id (previously a
    silent no-op because model_dump(exclude_none=True) dropped the null value
    before the runtime-change detection ran — CRITICAL fix for Phase 15)."""
    rt = await _make_runtime(async_session, slug="to-clear")
    agent = Agent(
        name=f"NullTest-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        runtime_id=rt.id,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.runtime_id == rt.id  # pre-condition

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}",
        json={"runtime_id": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runtime_id"] is None

    # Confirm DB state was actually updated.
    await async_session.refresh(agent)
    assert agent.runtime_id is None
