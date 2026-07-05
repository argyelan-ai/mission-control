"""Tests for the second switch axis — `new_harness` (ADR-056, Task 5).

Covers the harness/provider decoupling behaviour on top of
`agent_runtime_switch.switch_agent_runtime`:

  1. incompatible harness × runtime  → RuntimeIncompatibleError
  2. provider switch, same harness    → no image change, harness preserved
  3. harness change on same runtime    → forced image change
  4. health failure                    → rollback restores harness + runtime
  5. legacy NULL harness               → derived from target on first switch

Helpers + fixtures mirror `tests/test_agent_runtime_switch.py` 1:1.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch, AsyncMock

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.agent_runtime_switch import (
    RuntimeIncompatibleError,
    SwitchHealthCheckFailed,
    switch_agent_runtime,
)


# ── auto-redis fixture (identical to test_agent_runtime_switch.py) ──────────


@pytest.fixture(autouse=True)
def _patched_redis(fake_redis):
    async def _async_get_redis():
        return fake_redis
    with patch("app.services.agent_runtime_switch.get_redis", _async_get_redis), \
         patch("app.services.sse.get_redis", _async_get_redis), \
         patch("app.redis_client.get_redis", _async_get_redis):
        yield fake_redis


# ── helpers ────────────────────────────────────────────────────────────────


async def _mk_runtime(
    session,
    *,
    slug="rt",
    runtime_type="lmstudio",
    enabled=True,
    supports_tools=False,
):
    rt = Runtime(
        slug=slug,
        display_name=f"RT {slug}",
        runtime_type=runtime_type,
        endpoint="http://example.com/v1",
        model_identifier=f"model-{slug}",
        enabled=enabled,
        supports_tools=supports_tools,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _mk_agent(
    session,
    *,
    agent_runtime="cli-bridge",
    runtime_id=None,
    current_task_id=None,
    cli_plugins=None,
):
    a = Agent(
        name=f"A-{uuid.uuid4().hex[:6]}",
        agent_runtime=agent_runtime,
        runtime_id=runtime_id,
        current_task_id=current_task_id,
        cli_plugins=cli_plugins,
    )
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return a


def _side_effect_patches(*, health_ok=True):
    """The four external side-effects, patched with success defaults.

    Same stubs as test_agent_runtime_switch.py; only the health payload is
    parameterised so a test can force the rollback path.
    """
    health_payload = (
        {"healthy": True, "reason": "ok"}
        if health_ok
        else {"healthy": False, "reason": "boom"}
    )
    return [
        patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})),
        patch(
            "app.services.agent_runtime_switch.restart_docker_agent_container",
            side_effect=lambda *a, **k: {"status": "restarted", "container": "x", "mode": "restart"},
        ),
        patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value=health_payload)),
        patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})),
    ]


# ── 1. Incompatible harness × runtime raises ───────────────────────────────


@pytest.mark.asyncio
async def test_incompatible_harness_raises(async_session):
    rt = await _mk_runtime(async_session, slug="cloud-x", runtime_type="cloud")
    agent = await _mk_agent(async_session, runtime_id=rt.id, cli_plugins=[])
    with pytest.raises(RuntimeIncompatibleError):
        await switch_agent_runtime(async_session, agent, rt.id, new_harness="claude")


# ── 2. Provider switch, same harness → no image change ─────────────────────


@pytest.mark.asyncio
async def test_provider_switch_same_harness_no_image_change(async_session):
    rt_old = await _mk_runtime(async_session, slug="omp-local", runtime_type="omp")
    rt_new = await _mk_runtime(async_session, slug="ollama-c", runtime_type="cloud")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])
    agent.harness = "omp"
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    p = _side_effect_patches()
    with p[0], p[1], p[2], p[3]:
        result = await switch_agent_runtime(async_session, agent, rt_new.id)

    assert result.image_switched is False  # omp stays omp
    assert agent.harness == "omp"
    assert result.to_dict()["harness"] == "omp"
    assert result.to_dict()["old_harness"] == "omp"


# ── 3. Harness change on same runtime forces image change ──────────────────


@pytest.mark.asyncio
async def test_harness_change_forces_image_change(async_session):
    rt = await _mk_runtime(async_session, slug="cloud-y", runtime_type="cloud")
    agent = await _mk_agent(async_session, runtime_id=rt.id, cli_plugins=[])
    agent.harness = "omp"
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    p = _side_effect_patches()
    with p[0], p[1], p[2], p[3]:
        result = await switch_agent_runtime(
            async_session, agent, rt.id, new_harness="openclaude"
        )

    assert result.image_switched is True
    assert agent.harness == "openclaude"
    assert result.to_dict()["harness"] == "openclaude"
    assert result.to_dict()["old_harness"] == "omp"


# ── 4. Rollback restores harness + runtime on health failure ───────────────


@pytest.mark.asyncio
async def test_rollback_restores_harness(async_session):
    rt_old = await _mk_runtime(async_session, slug="omp-l2", runtime_type="omp")
    rt_new = await _mk_runtime(async_session, slug="cloud-z", runtime_type="cloud")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])
    agent.harness = "omp"
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    p = _side_effect_patches(health_ok=False)
    with p[0], p[1], p[2], p[3]:
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(
                async_session, agent, rt_new.id, new_harness="openclaude"
            )

    await async_session.refresh(agent)
    assert agent.harness == "omp"
    assert agent.runtime_id == rt_old.id


# ── 5. Legacy NULL harness materialises from target on first switch ────────


@pytest.mark.asyncio
async def test_legacy_null_harness_derives_from_target(async_session):
    rt_old = await _mk_runtime(async_session, slug="lms-a", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="vllm-b", runtime_type="vllm_docker")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])  # harness None

    p = _side_effect_patches()
    with p[0], p[1], p[2], p[3]:
        result = await switch_agent_runtime(async_session, agent, rt_new.id)

    await async_session.refresh(agent)
    assert agent.harness == "openclaude"  # materialised on first switch
    assert result.image_switched is False
    assert result.to_dict()["harness"] == "openclaude"
