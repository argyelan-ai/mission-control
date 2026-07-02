"""Tests for agent_runtime_switch.py — Phase 15 Wave 2.

Coverage:
  1.  same-image switch → restart only (no force_recreate)
  2.  cross-image switch → force_recreate path
  3.  dry_run → no DB / file / restart side-effects
  4.  in_progress without force → AgentBusyError
  5.  in_progress with force → success
  6.  disabled runtime → RuntimeIncompatibleError
  7.  host agent → AgentNotSwitchableError
  8.  health check failure → rollback + SwitchHealthCheckFailed
  9.  concurrent switch → RuntimeSwitchLockTimeout
 10.  success → emits agent.runtime_switched activity event
 11.  failure → emits agent.runtime_switch_failed
 12.  warnings populated for tools-vs-no-tools mismatch
"""
from __future__ import annotations

import uuid
from unittest.mock import patch, AsyncMock

import pytest

from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.agent_runtime_switch import (
    AgentBusyError,
    AgentNotSwitchableError,
    RuntimeIncompatibleError,
    RuntimeNotFoundError,
    RuntimeSwitchLockTimeout,
    SwitchHealthCheckFailed,
    is_agent_busy,
    switch_agent_runtime,
    validate_compatibility,
)
from sqlmodel import select


# ── auto-redis fixture ─────────────────────────────────────────────────────
# All tests in this module use the service directly (no FastAPI HTTP layer)
# so the conftest's `client` fixture isn't running its `get_redis` overrides.
# Patch `get_redis` at every call-site that the switch service touches:
# the switch service itself + activity.emit_event (which calls broadcast →
# sse.get_redis).


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


def _patch_side_effects(
    *,
    sync_ok=True,
    restart_status="restarted",
    health_ok=True,
    write_compose_ok=True,
):
    """Stack the four external side-effects with sensible defaults."""
    sync_mock = AsyncMock(return_value={"SOUL.md": "written"} if sync_ok else {})
    if not sync_ok:
        sync_mock.side_effect = RuntimeError("sync boom")
    restart_mock = lambda agent, *, force_recreate=False, respawn_window_only=False: {
        "status": restart_status,
        "container": f"mc-agent-{agent.name.lower()}",
        "mode": "recreate" if force_recreate else ("respawn" if respawn_window_only else "restart"),
    }
    if not health_ok:
        health_payload = {"healthy": False, "reason": "stub timeout"}
    else:
        health_payload = {"healthy": True, "reason": "stub ok"}
    health_mock = AsyncMock(return_value=health_payload)
    if write_compose_ok:
        compose_mock = AsyncMock(return_value={"path": "x", "backup": "x.bak", "bytes": "10", "changed": "true"})
    else:
        compose_mock = AsyncMock(side_effect=RuntimeError("compose boom"))

    return [
        patch("app.services.agent_runtime_switch.sync_docker_agent_files", sync_mock),
        patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=None, new=restart_mock),
        patch("app.services.agent_runtime_switch.wait_for_agent_healthy", health_mock),
        patch("app.services.agent_runtime_switch.write_compose_agents", compose_mock),
    ]


# ── 1. Same-image switch ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_image_switch_calls_restart_only(async_session):
    rt_old = await _mk_runtime(async_session, slug="lms-old", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="vllm-new", runtime_type="vllm_docker")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    captured: dict = {}

    def fake_restart(a, *, force_recreate=False, respawn_window_only=False):
        captured["force_recreate"] = force_recreate
        captured["respawn_window_only"] = respawn_window_only
        return {"status": "restarted", "container": "x", "mode": "restart"}

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=fake_restart), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})):
        result = await switch_agent_runtime(async_session, agent, rt_new.id)

    assert result.image_switched is False
    assert captured["force_recreate"] is False
    assert result.new_runtime["slug"] == "vllm-new"


# ── 2. Cross-image switch ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_image_switch_calls_force_recreate(async_session):
    rt_cloud = await _mk_runtime(async_session, slug="anthropic-claude-test", runtime_type="anthropic_api")
    rt_vllm = await _mk_runtime(async_session, slug="vllm", runtime_type="vllm_docker")
    agent = await _mk_agent(async_session, runtime_id=rt_cloud.id, cli_plugins=[])

    captured: dict = {}

    def fake_restart(a, *, force_recreate=False, respawn_window_only=False):
        captured["force_recreate"] = force_recreate
        captured["respawn_window_only"] = respawn_window_only
        return {"status": "recreated", "container": "x", "mode": "recreate"}

    compose_mock = AsyncMock(return_value={"changed": "true"})
    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=fake_restart), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", compose_mock):
        result = await switch_agent_runtime(async_session, agent, rt_vllm.id)

    assert result.image_switched is True
    assert captured["force_recreate"] is True
    compose_mock.assert_awaited()  # rendered before restart


# ── 3. Dry-run ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_no_mutation(async_session):
    rt_old = await _mk_runtime(async_session, slug="old", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="new", runtime_type="vllm_docker")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])
    original_runtime_id = agent.runtime_id

    sync_mock = AsyncMock()
    restart_mock_calls: list = []
    compose_mock = AsyncMock()

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", sync_mock), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=lambda *a, **k: restart_mock_calls.append(k) or {}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock()), \
         patch("app.services.agent_runtime_switch.write_compose_agents", compose_mock):
        result = await switch_agent_runtime(async_session, agent, rt_new.id, dry_run=True)

    assert result.dry_run is True
    assert result.image_switched is False  # both → mc-agent-base, no image change
    sync_mock.assert_not_awaited()
    compose_mock.assert_not_awaited()
    assert restart_mock_calls == []
    # DB untouched
    await async_session.refresh(agent)
    assert agent.runtime_id == original_runtime_id


# ── 4. In-progress without force → busy ────────────────────────────────────


@pytest.mark.asyncio
async def test_in_progress_raises_busy(async_session):
    rt_new = await _mk_runtime(async_session, slug="new", runtime_type="lmstudio")
    fake_task_id = uuid.uuid4()
    agent = await _mk_agent(async_session, current_task_id=fake_task_id, cli_plugins=[])

    with pytest.raises(AgentBusyError) as exc:
        await switch_agent_runtime(async_session, agent, rt_new.id)
    assert exc.value.current_task_id == fake_task_id


# ── 5. In-progress with force → success ────────────────────────────────────


@pytest.mark.asyncio
async def test_in_progress_force_succeeds(async_session):
    rt_new = await _mk_runtime(async_session, slug="new", runtime_type="lmstudio")
    agent = await _mk_agent(async_session, current_task_id=uuid.uuid4(), cli_plugins=[])

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=lambda *a, **k: {"status": "restarted", "container": "x", "mode": "restart"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})):
        result = await switch_agent_runtime(
            async_session, agent, rt_new.id, force_when_in_progress=True
        )
    assert result.new_runtime["slug"] == "new"


# ── 6. Disabled runtime ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_runtime_raises_incompatible(async_session):
    rt = await _mk_runtime(async_session, slug="off", enabled=False)
    agent = await _mk_agent(async_session, cli_plugins=[])

    with pytest.raises(RuntimeIncompatibleError):
        await switch_agent_runtime(async_session, agent, rt.id)


# ── 7. Host agent reject ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_host_agent_rejected_with_clear_message(async_session):
    rt = await _mk_runtime(async_session)
    agent = await _mk_agent(async_session, agent_runtime="host", cli_plugins=[])

    with pytest.raises(AgentNotSwitchableError) as exc:
        await switch_agent_runtime(async_session, agent, rt.id)
    assert "cli-bridge" in str(exc.value)


# ── 8. Health failure → rollback ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_failure_triggers_rollback(async_session):
    rt_old = await _mk_runtime(async_session, slug="anthropic-claude-old", runtime_type="anthropic_api")
    rt_new = await _mk_runtime(async_session, slug="new-oc", runtime_type="vllm_docker")  # cross-image (slug-based)
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    sync_calls: list = []

    async def fake_sync(session, agent_arg):
        sync_calls.append(agent_arg.runtime_id)
        return {}

    restart_calls: list = []

    def fake_restart(a, *, force_recreate=False, respawn_window_only=False):
        restart_calls.append(force_recreate)
        return {"status": "recreated", "container": "x", "mode": "recreate"}

    compose_calls: list = []

    async def fake_compose(session):
        compose_calls.append(True)
        return {"changed": "true"}

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", side_effect=fake_sync), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=fake_restart), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": False, "reason": "timeout"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", side_effect=fake_compose):
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_new.id)

    # DB rolled back
    await async_session.refresh(agent)
    assert agent.runtime_id == rt_old.id

    # Sync was called both during the forward attempt and the rollback.
    assert len(sync_calls) >= 2
    # Compose rendered twice (forward + rollback).
    assert len(compose_calls) >= 2
    # Restart called at least once forward + once during rollback.
    assert len(restart_calls) >= 2

    # Failure event recorded.
    events = (await async_session.exec(select(ActivityEvent))).all()
    assert any(e.event_type == "agent.runtime_switch_failed" for e in events)


# ── 9. Concurrent switch → lock timeout ────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_switch_lock_timeout(async_session):
    rt_new = await _mk_runtime(async_session, slug="new", runtime_type="lmstudio")
    agent = await _mk_agent(async_session, cli_plugins=[])

    from app.redis_client import get_redis
    redis = await get_redis()
    # Pre-take the lock to simulate concurrent switch in flight.
    await redis.set(f"mc:agent:{agent.id}:runtime-switch", "1", nx=True, ex=120)

    try:
        with pytest.raises(RuntimeSwitchLockTimeout):
            await switch_agent_runtime(async_session, agent, rt_new.id)
    finally:
        await redis.delete(f"mc:agent:{agent.id}:runtime-switch")


# ── 10. Success event ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emits_activity_event_on_success(async_session):
    rt_old = await _mk_runtime(async_session, slug="old", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="new", runtime_type="lmstudio")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=lambda *a, **k: {"status": "restarted", "container": "x", "mode": "restart"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})):
        await switch_agent_runtime(async_session, agent, rt_new.id)

    events = (await async_session.exec(select(ActivityEvent))).all()
    assert any(e.event_type == "agent.runtime_switched" for e in events)


# ── 11. Failure event (already covered in #8 but explicit) ─────────────────


@pytest.mark.asyncio
async def test_emits_failure_event_on_compose_render_error(async_session):
    rt_old = await _mk_runtime(async_session, slug="anthropic-claude-old2", runtime_type="anthropic_api")
    rt_new = await _mk_runtime(async_session, slug="new-oc2", runtime_type="vllm_docker")  # cross-image (slug-based)
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    with patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(side_effect=RuntimeError("compose boom"))), \
         patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=lambda *a, **k: {"status": "restarted", "container": "x", "mode": "restart"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})):
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_new.id)

    # DB rollback + failure event.
    await async_session.refresh(agent)
    assert agent.runtime_id == rt_old.id
    events = (await async_session.exec(select(ActivityEvent))).all()
    assert any(e.event_type == "agent.runtime_switch_failed" for e in events)


# ── 12. Warnings populated ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warnings_collected_for_tool_runtime_mismatch(async_session):
    """Agent uses tools (cli_plugins=None → all enabled), runtime says
    supports_tools=False — switch succeeds but warnings are surfaced."""
    rt = await _mk_runtime(async_session, slug="no-tools", supports_tools=False)
    agent = await _mk_agent(async_session, cli_plugins=None)

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=lambda *a, **k: {"status": "restarted", "container": "x", "mode": "restart"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})):
        result = await switch_agent_runtime(async_session, agent, rt.id)

    assert result.warnings
    assert any("tool" in w.lower() for w in result.warnings)


# ── bonus sanity: 404 + busy helper ────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_not_found(async_session):
    agent = await _mk_agent(async_session, cli_plugins=[])
    with pytest.raises(RuntimeNotFoundError):
        await switch_agent_runtime(async_session, agent, uuid.uuid4())


@pytest.mark.asyncio
async def test_is_agent_busy_helper(async_session):
    a1 = await _mk_agent(async_session, cli_plugins=[])
    a2 = await _mk_agent(async_session, current_task_id=uuid.uuid4(), cli_plugins=[])
    assert is_agent_busy(a1) is False
    assert is_agent_busy(a2) is True


# ── 13–15. Rollback restart failure surfaces (HIGH-3 fix) ──────────────────


def _patch_rollback_restart_fail(restart_side_effect=RuntimeError("container down")):
    """Patches that make the *rollback* restart raise while the forward path
    also fails (health check → False triggers rollback).  Discord is always
    mocked to avoid HTTP calls in tests."""
    sync_mock = AsyncMock(return_value={})

    call_count = {"n": 0}

    def fake_restart(a, *, force_recreate=False, respawn_window_only=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Forward restart: succeed so we get to health check.
            return {"status": "recreated", "container": "x", "mode": "recreate"}
        # Rollback restart: raise to trigger the new error-surfacing code.
        raise restart_side_effect

    compose_mock = AsyncMock(return_value={"changed": "true"})
    discord_mock = AsyncMock()

    return [
        patch("app.services.agent_runtime_switch.sync_docker_agent_files", sync_mock),
        patch("app.services.agent_runtime_switch.restart_docker_agent_container",
              side_effect=fake_restart),
        patch("app.services.agent_runtime_switch.wait_for_agent_healthy",
              AsyncMock(return_value={"healthy": False, "reason": "timeout"})),
        patch("app.services.agent_runtime_switch.write_compose_agents", compose_mock),
        patch("app.services.agent_runtime_switch.send_discord_notification", discord_mock),
    ], discord_mock


@pytest.mark.asyncio
async def test_rollback_restart_fail_emits_error_event(async_session):
    """When rollback restart raises, an agent.runtime_rollback_failed activity
    event with severity=error must be emitted."""
    rt_old = await _mk_runtime(async_session, slug="old", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="new", runtime_type="cloud")  # cross-image
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    patches, _ = _patch_rollback_restart_fail()
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_new.id)

    events = (await async_session.exec(select(ActivityEvent))).all()
    rollback_events = [e for e in events if e.event_type == "agent.runtime_rollback_failed"]
    assert rollback_events, "Expected agent.runtime_rollback_failed event, got none"
    assert rollback_events[0].severity == "error"
    assert rollback_events[0].detail["rollback_status"] == "container_unreachable"


@pytest.mark.asyncio
async def test_rollback_restart_fail_notifies_discord(async_session):
    """When rollback restart raises, send_discord_notification must be called
    with severity=error so the operator receives an ops alert."""
    rt_old = await _mk_runtime(async_session, slug="old2", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="new2", runtime_type="cloud")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    patches, discord_mock = _patch_rollback_restart_fail()
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_new.id)

    discord_mock.assert_awaited_once()
    call_kwargs = discord_mock.call_args.kwargs
    assert call_kwargs.get("severity") == "error"
    assert agent.name in call_kwargs.get("title", "")


@pytest.mark.asyncio
async def test_rollback_restart_fail_sets_provision_error_state(async_session):
    """When rollback restart raises, agent.provision_status must be set to
    'error' in the DB so AgentCard shows a red error badge in the UI."""
    rt_old = await _mk_runtime(async_session, slug="old3", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="new3", runtime_type="cloud")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    patches, _ = _patch_rollback_restart_fail()
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        with pytest.raises(SwitchHealthCheckFailed):
            await switch_agent_runtime(async_session, agent, rt_new.id)

    await async_session.refresh(agent)
    assert agent.provision_status == "error", (
        f"Expected provision_status='error', got '{agent.provision_status}'"
    )


# ── 16. Phase 16 D-11: respawn_window_only wired for same-image switch ────


@pytest.mark.asyncio
async def test_same_image_switch_calls_respawn(async_session):
    """D-11: Same-image switch must call restart with respawn_window_only=True
    and force_recreate=False."""
    rt_old = await _mk_runtime(async_session, slug="lms-a", runtime_type="lmstudio")
    rt_new = await _mk_runtime(async_session, slug="lms-b", runtime_type="lmstudio")
    agent = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    captured: dict = {}

    def fake_restart(a, *, force_recreate=False, respawn_window_only=False):
        captured["force_recreate"] = force_recreate
        captured["respawn_window_only"] = respawn_window_only
        return {"status": "respawned", "container": "x", "mode": "respawn"}

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=fake_restart), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})):
        result = await switch_agent_runtime(async_session, agent, rt_new.id)

    assert result.image_switched is False
    assert captured["force_recreate"] is False
    assert captured["respawn_window_only"] is True


# ── 17. Phase 16 D-11: cross-image switch keeps force_recreate path ────────


@pytest.mark.asyncio
async def test_cross_image_switch_no_respawn(async_session):
    """D-11: Cross-image switch must call restart with force_recreate=True
    and respawn_window_only=False."""
    rt_cloud = await _mk_runtime(async_session, slug="anthropic-claude-cl", runtime_type="anthropic_api")
    rt_vllm = await _mk_runtime(async_session, slug="vl", runtime_type="vllm_docker")
    agent = await _mk_agent(async_session, runtime_id=rt_cloud.id, cli_plugins=[])

    captured: dict = {}

    def fake_restart(a, *, force_recreate=False, respawn_window_only=False):
        captured["force_recreate"] = force_recreate
        captured["respawn_window_only"] = respawn_window_only
        return {"status": "recreated", "container": "x", "mode": "recreate"}

    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container", side_effect=fake_restart), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", AsyncMock(return_value={"healthy": True, "reason": "ok"})), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        result = await switch_agent_runtime(async_session, agent, rt_vllm.id)

    assert result.image_switched is True
    assert captured["force_recreate"] is True
    assert captured["respawn_window_only"] is False


# ── 18. Phase 16 D-12: respawn_mode + timeout per image_change ─────────────


@pytest.mark.asyncio
async def test_respawn_mode_used_for_health_check(async_session):
    """D-12: wait_for_agent_healthy must receive respawn_mode=True for
    same-image switches with timeout=30, respawn_mode=False with timeout=90
    (HEALTH_TIMEOUT_RECREATE) for cross-image switches."""
    # Same-image case
    rt_old = await _mk_runtime(async_session, slug="same-old", runtime_type="lmstudio")
    rt_new_same = await _mk_runtime(async_session, slug="same-new", runtime_type="lmstudio")
    agent_same = await _mk_agent(async_session, runtime_id=rt_old.id, cli_plugins=[])

    same_health = AsyncMock(return_value={"healthy": True, "reason": "ok"})
    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container",
               side_effect=lambda *a, **k: {"status": "respawned", "container": "x", "mode": "respawn"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", same_health), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "false"})):
        await switch_agent_runtime(async_session, agent_same, rt_new_same.id)

    same_health.assert_awaited_once()
    same_kwargs = same_health.call_args.kwargs
    assert same_kwargs.get("respawn_mode") is True
    assert same_kwargs.get("timeout") == 30

    # Cross-image case
    rt_cl = await _mk_runtime(async_session, slug="anthropic-claude-cross", runtime_type="anthropic_api")
    rt_vl = await _mk_runtime(async_session, slug="cross-vl", runtime_type="vllm_docker")
    agent_cross = await _mk_agent(async_session, runtime_id=rt_cl.id, cli_plugins=[])

    cross_health = AsyncMock(return_value={"healthy": True, "reason": "ok"})
    with patch("app.services.agent_runtime_switch.sync_docker_agent_files", AsyncMock(return_value={})), \
         patch("app.services.agent_runtime_switch.restart_docker_agent_container",
               side_effect=lambda *a, **k: {"status": "recreated", "container": "x", "mode": "recreate"}), \
         patch("app.services.agent_runtime_switch.wait_for_agent_healthy", cross_health), \
         patch("app.services.agent_runtime_switch.write_compose_agents", AsyncMock(return_value={"changed": "true"})):
        await switch_agent_runtime(async_session, agent_cross, rt_vl.id)

    cross_health.assert_awaited_once()
    cross_kwargs = cross_health.call_args.kwargs
    assert cross_kwargs.get("respawn_mode") is False
    assert cross_kwargs.get("timeout") == 90  # HEALTH_TIMEOUT_RECREATE
