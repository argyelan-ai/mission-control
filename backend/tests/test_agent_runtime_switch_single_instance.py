"""Tests for HERM-04 / D-08 / D-09 — single_instance hard-block in
``switch_agent_runtime`` (Phase 24 plan 03).

Two-direction guard:
  - Switching INTO a single_instance runtime → AgentNotSwitchableError
  - Switching OUT OF a single_instance runtime → AgentNotSwitchableError

Plus regression coverage:
  - Switch between two non-single_instance runtimes still works (no false-fire)
  - Pre-existing cli-bridge whitelist (`_ensure_agent_switchable`) still raises
    earlier on host/openclaw agents — generic block does not change semantics.

The Runtime DB column for ``single_instance`` ships with plan 24-01. This
test module is wave-1 sibling and must NOT depend on that migration: we set
the attribute directly on Runtime instances after construction. The service
reads via ``getattr(rt, "single_instance", False)`` so prod + tests stay
aligned.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch, AsyncMock

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.agent_runtime_switch import (
    AgentNotSwitchableError,
    switch_agent_runtime,
)


# ── auto-redis fixture (mirrors test_agent_runtime_switch.py) ─────────────


@pytest.fixture(autouse=True)
def _patched_redis(fake_redis):
    async def _async_get_redis():
        return fake_redis
    with patch("app.services.agent_runtime_switch.get_redis", _async_get_redis), \
         patch("app.services.sse.get_redis", _async_get_redis), \
         patch("app.redis_client.get_redis", _async_get_redis):
        yield fake_redis


# ── helpers ───────────────────────────────────────────────────────────────


async def _mk_runtime(
    session,
    *,
    slug: str,
    runtime_type: str = "lmstudio",
    enabled: bool = True,
    single_instance: bool = False,
) -> Runtime:
    rt = Runtime(
        slug=slug,
        display_name=f"RT {slug}",
        runtime_type=runtime_type,
        endpoint="http://example.com/v1",
        model_identifier=f"model-{slug}",
        enabled=enabled,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    # Set ad-hoc attribute (DB column lands in plan 24-01 migration).
    rt.single_instance = single_instance
    return rt


async def _mk_agent(
    session,
    *,
    agent_runtime: str = "cli-bridge",
    runtime_id: uuid.UUID | None = None,
) -> Agent:
    a = Agent(
        name=f"A-{uuid.uuid4().hex[:6]}",
        agent_runtime=agent_runtime,
        runtime_id=runtime_id,
        cli_plugins=[],
    )
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return a


def _stub_side_effects():
    """Stub the four restart-side-effects so happy-path tests don't touch
    docker/redis/compose."""
    return [
        patch(
            "app.services.agent_runtime_switch.sync_docker_agent_files",
            AsyncMock(return_value={}),
        ),
        patch(
            "app.services.agent_runtime_switch.restart_docker_agent_container",
            side_effect=lambda a, *, force_recreate=False, respawn_window_only=False: {
                "status": "restarted",
                "container": f"mc-agent-{a.name.lower()}",
                "mode": "restart",
            },
        ),
        patch(
            "app.services.agent_runtime_switch.wait_for_agent_healthy",
            AsyncMock(return_value={"healthy": True, "reason": "ok"}),
        ),
        patch(
            "app.services.agent_runtime_switch.write_compose_agents",
            AsyncMock(return_value={"changed": "false"}),
        ),
    ]


# ── 1. Target single_instance blocked ─────────────────────────────────────


@pytest.mark.asyncio
async def test_target_single_instance_runtime_blocks_switch(async_session):
    rt_a = await _mk_runtime(async_session, slug="a-multi", single_instance=False)
    rt_b = await _mk_runtime(async_session, slug="b-hermes", single_instance=True)
    agent = await _mk_agent(async_session, runtime_id=rt_a.id)

    with _patched_session_get(async_session, {rt_a.id: rt_a, rt_b.id: rt_b}):
        with pytest.raises(AgentNotSwitchableError) as excinfo:
            await switch_agent_runtime(async_session, agent, rt_b.id)

    assert "single_instance" in str(excinfo.value).lower()


# ── 2. Source single_instance blocked ─────────────────────────────────────


@pytest.mark.asyncio
async def test_source_single_instance_runtime_blocks_switch(async_session):
    rt_a = await _mk_runtime(async_session, slug="a-hermes-src", single_instance=True)
    rt_b = await _mk_runtime(async_session, slug="b-multi-target", single_instance=False)
    agent = await _mk_agent(async_session, runtime_id=rt_a.id)

    with _patched_session_get(async_session, {rt_a.id: rt_a, rt_b.id: rt_b}):
        with pytest.raises(AgentNotSwitchableError) as excinfo:
            await switch_agent_runtime(async_session, agent, rt_b.id)

    assert "single_instance" in str(excinfo.value).lower()


# ── 3. Happy path — two non-single_instance runtimes still switch ─────────


@pytest.mark.asyncio
async def test_non_single_instance_switch_succeeds(async_session):
    rt_a = await _mk_runtime(async_session, slug="multi-a", single_instance=False)
    rt_b = await _mk_runtime(async_session, slug="multi-b", single_instance=False)
    agent = await _mk_agent(async_session, runtime_id=rt_a.id)

    patches = _stub_side_effects()
    for p in patches:
        p.start()
    try:
        # Use real session.get — flag defaults to False on freshly-read rows
        # which is exactly what the service should observe in prod for any
        # non-single_instance row, even pre-migration.
        result = await switch_agent_runtime(async_session, agent, rt_b.id)
    finally:
        for p in patches:
            p.stop()

    assert result.dry_run is False
    assert result.new_runtime["slug"] == "multi-b"


# ── 4. Existing cli-bridge whitelist still raises earlier (regression) ────


@pytest.mark.asyncio
async def test_host_agent_still_blocked_by_existing_whitelist(async_session):
    rt_a = await _mk_runtime(async_session, slug="any", single_instance=False)
    agent = await _mk_agent(async_session, agent_runtime="host", runtime_id=None)

    with pytest.raises(AgentNotSwitchableError) as excinfo:
        await switch_agent_runtime(async_session, agent, rt_a.id)

    msg = str(excinfo.value)
    # Existing cli-bridge whitelist message — single_instance check must NOT
    # have fired (it would say "single_instance" instead).
    assert "Runtime-Switch nicht unterstuetzt" in msg
    assert "single_instance" not in msg.lower()


# ── 5. Router-level 422 mapping (verified by static check) ────────────────


def test_router_maps_agent_not_switchable_to_422():
    """``PATCH /agents/{id}`` must surface AgentNotSwitchableError as HTTP 422.

    We check by reading the source — the router has the mapping inline in
    two places (real switch + dry-run preview). Plan 24-03 acceptance
    criteria allow either runtime test or static grep verification.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py"
    text = src.read_text()
    # Look for the canonical pattern — both `except AgentNotSwitchableError`
    # and the 422 status code on the same handler block.
    assert "except AgentNotSwitchableError" in text
    # crude proximity check — 422 must appear within 3 lines of the except.
    lines = text.splitlines()
    hits = [
        i for i, ln in enumerate(lines)
        if "except AgentNotSwitchableError" in ln
    ]
    assert hits, "AgentNotSwitchableError handler not found in router"
    for idx in hits:
        window = "\n".join(lines[idx : idx + 4])
        assert "422" in window, f"422 mapping missing near line {idx + 1}"


# ── helper for ad-hoc-flag session.get patch ──────────────────────────────


from contextlib import contextmanager


@contextmanager
def _patched_session_get(session, flagged_runtimes: dict):
    """Patch ``session.get`` (instance-bound) so Runtime lookups for known
    ids return our test instances (which carry the ad-hoc ``single_instance``
    attribute set in ``_mk_runtime``). Other lookups fall through to the
    original bound method.
    """
    original_get = session.get

    async def _get(model_cls, key, *args, **kwargs):
        if model_cls is Runtime and key in flagged_runtimes:
            return flagged_runtimes[key]
        return await original_get(model_cls, key, *args, **kwargs)

    session.get = _get  # type: ignore[method-assign]
    try:
        yield
    finally:
        session.get = original_get  # type: ignore[method-assign]
