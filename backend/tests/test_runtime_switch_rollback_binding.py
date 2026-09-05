"""Runtime-Switch: der Rollback darf die Runtime-Bindung nie verlieren.

Vorfall 2026-09-05 (Agent auf omp/cli-bridge, zwei gescheiterte Switches im
Abstand von 4 Minuten):

  1. Switch A → B, Health-Check scheitert, Rollback stellt A wieder her. ✅
  2. Dazwischen setzt ein PATCH mit ``{"runtime_id": null}`` die Bindung
     still auf NULL (kein Event, kein Log — routers/agents.py "explicit unset").
  3. Zweiter Switch startet also ohne Bindung, scheitert wieder am
     Health-Check, und der Rollback schreibt getreu NULL zurueck.
  4. Force-Recreate danach → Bootstrap ohne Runtime → der omp-Entrypoint
     bricht mit FATAL ab (docker/omp-bridge/entrypoint.sh) → Crash-Loop.

Diese Tests halten drei Invarianten fest:
  * zwei gescheiterte Switches hintereinander lassen die Bindung auf A,
  * ein Rollback macht einen omp-Agenten nie bindungslos,
  * ein explizites Unset ist fuer omp-Agenten verboten und sonst sichtbar.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.agent_runtime_switch import (
    SwitchHealthCheckFailed,
    switch_agent_runtime,
)


@pytest.fixture(autouse=True)
def _patched_redis(fake_redis):
    async def _async_get_redis():
        return fake_redis

    with patch("app.services.agent_runtime_switch.get_redis", _async_get_redis), \
         patch("app.services.sse.get_redis", _async_get_redis), \
         patch("app.redis_client.get_redis", _async_get_redis):
        yield fake_redis


async def _mk_runtime(session, *, slug: str, runtime_type: str = "lmstudio") -> Runtime:
    rt = Runtime(
        slug=slug,
        display_name=f"RT {slug}",
        runtime_type=runtime_type,
        endpoint="http://box-a.invalid/v1",
        model_identifier=f"model-{slug}",
        enabled=True,
        supports_tools=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _mk_agent(session, *, runtime_id=None, harness="omp") -> Agent:
    agent = Agent(
        name=f"agent-a-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        harness=harness,
        runtime_id=runtime_id,
        cli_plugins=[],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _failing_health_patches():
    """Alle vier Nebenwirkungen gestubbt, Health-Check schlaegt fehl."""
    return [
        patch(
            "app.services.agent_runtime_switch.sync_docker_agent_files",
            AsyncMock(return_value={}),
        ),
        patch(
            "app.services.agent_runtime_switch.restart_docker_agent_container",
            side_effect=lambda *a, **k: {
                "status": "restarted",
                "container": "mc-agent-a",
                "mode": "restart",
            },
        ),
        patch(
            "app.services.agent_runtime_switch.wait_for_agent_healthy",
            AsyncMock(
                return_value={
                    "healthy": False,
                    "reason": "timeout after 30s — window not ready",
                }
            ),
        ),
        patch(
            "app.services.agent_runtime_switch.write_compose_agents",
            AsyncMock(return_value={"changed": "false"}),
        ),
    ]


@pytest.mark.asyncio
async def test_two_failed_switches_keep_binding_on_a(async_session):
    """Zweimal derselbe gescheiterte Switch — die Bindung bleibt auf A."""
    rt_a = await _mk_runtime(async_session, slug="recipe-x-a")
    rt_b = await _mk_runtime(async_session, slug="recipe-x-b")
    agent = await _mk_agent(async_session, runtime_id=rt_a.id)

    for _ in range(2):
        patches = _failing_health_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(SwitchHealthCheckFailed):
                await switch_agent_runtime(async_session, agent, rt_b.id)
        finally:
            for p in patches:
                p.stop()

    await async_session.refresh(agent)
    assert agent.runtime_id == rt_a.id
    assert agent.harness == "omp"


@pytest.mark.asyncio
async def test_rollback_does_not_unbind_omp_agent(async_session):
    """Startet der Switch ohne Bindung, darf der Rollback sie nicht zementieren.

    Ein omp-Agent ohne Runtime kann nicht booten (Entrypoint FATAL). Die
    zuletzt versuchte Bindung ist die einzige, mit der der Container ueberhaupt
    startet — der Rollback behaelt sie und meldet das laut.
    """
    rt_b = await _mk_runtime(async_session, slug="recipe-x-b")
    agent = await _mk_agent(async_session, runtime_id=None)

    patches = _failing_health_patches()
    for p in patches:
        p.start()
    try:
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_b.id)
    finally:
        for p in patches:
            p.stop()

    await async_session.refresh(agent)
    assert agent.runtime_id == rt_b.id, "omp-Agent darf nach Rollback nicht bindungslos sein"

    events = (await async_session.exec(select(ActivityEvent))).all()
    assert any(e.event_type == "agent.runtime_rollback_kept_binding" for e in events)


@pytest.mark.asyncio
async def test_rollback_still_restores_none_for_claude_agent(async_session):
    """Fuer Harnesses mit Compose-Fallback bleibt das alte Verhalten."""
    rt_b = await _mk_runtime(async_session, slug="recipe-x-b", runtime_type="lmstudio")
    agent = await _mk_agent(async_session, runtime_id=None, harness="openclaude")

    patches = _failing_health_patches()
    for p in patches:
        p.start()
    try:
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_b.id)
    finally:
        for p in patches:
            p.stop()

    await async_session.refresh(agent)
    assert agent.runtime_id is None


@pytest.mark.asyncio
async def test_patch_unset_runtime_refused_for_omp_agent(auth_client, async_session):
    """PATCH {"runtime_id": null} auf einem omp-Agenten → 422, Bindung bleibt."""
    rt_a = await _mk_runtime(async_session, slug="recipe-x-a")
    agent = await _mk_agent(async_session, runtime_id=rt_a.id)

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}", json={"runtime_id": None}
    )
    assert resp.status_code == 422, resp.text
    assert "omp" in resp.json()["detail"].lower()

    await async_session.refresh(agent)
    assert agent.runtime_id == rt_a.id


@pytest.mark.asyncio
async def test_patch_unset_runtime_emits_event_for_claude_agent(auth_client, async_session):
    """Erlaubtes Unset bleibt erlaubt — aber nicht mehr still."""
    rt_a = await _mk_runtime(async_session, slug="recipe-x-a")
    agent = await _mk_agent(async_session, runtime_id=rt_a.id, harness="claude")

    resp = await auth_client.patch(
        f"/api/v1/agents/{agent.id}", json={"runtime_id": None}
    )
    assert resp.status_code == 200, resp.text

    await async_session.refresh(agent)
    assert agent.runtime_id is None

    events = (await async_session.exec(select(ActivityEvent))).all()
    assert any(e.event_type == "agent.runtime_unbound" for e in events)


@pytest.mark.asyncio
async def test_bootstrap_warns_when_omp_agent_has_no_runtime(client, caplog):
    """Bootstrap ohne Bindung: der Grund steht im Log, statt still zu bleiben."""
    import logging

    from app.models.secret import Secret
    from app.services.encryption import encrypt
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    name = f"agent-a-{uuid.uuid4().hex[:6]}"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            Agent(
                id=uuid.uuid4(),
                name=name,
                agent_runtime="cli-bridge",
                harness="omp",
                runtime_id=None,
            )
        )
        # Mindestens ein Token, sonst antwortet der Endpunkt mit 404.
        s.add(
            Secret(
                key="github_token",
                encrypted_value=encrypt("gh-test-token"),
                provider="github",
            )
        )
        await s.commit()

    with caplog.at_level(logging.WARNING, logger="mc.internal"):
        resp = await client.get(f"/api/v1/internal/bootstrap?agent_name={name}")

    assert resp.status_code == 200, resp.text
    assert "OPENAI_BASE_URL" not in resp.json()
    assert any(
        "Runtime-Bindung" in rec.getMessage() for rec in caplog.records
    ), caplog.text
