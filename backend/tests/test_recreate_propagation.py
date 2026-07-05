"""Recreate propagation (CLI-Tool-Updates) — idle recreate, busy flagging,
effective-harness targeting, circuit breaker, switch-lock guard."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services import agent_runtime_switch as switch_mod
from app.services import runtime_propagation as rp
from app.services import sse as sse_mod


async def _mk_rt(session, *, slug="rec-rt", runtime_type="vllm_docker",
                 model="a-model"):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type=runtime_type,
        endpoint="http://spark:8000/v1", model_identifier=model, enabled=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _mk_agent(session, rt, *, name="Sparky", agent_runtime="cli-bridge",
                    harness=None, busy=False, pending=False):
    agent = Agent(name=name, role="developer", agent_runtime=agent_runtime,
                  runtime_id=rt.id if rt else None, harness=harness,
                  pending_recreate=pending)
    if busy:
        agent.current_task_id = uuid.uuid4()
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis
    return _get


# ── mark_agents_for_recreate ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_flags_cli_bridge_matching_explicit_harness(async_session):
    """agent.harness wins over derive_harness for effective-harness matching."""
    rt = await _mk_rt(async_session)  # vllm_docker → derives 'openclaude'
    # Explicit harness 'omp' must be matched by harness='omp', not 'openclaude'.
    omp_agent = await _mk_agent(async_session, rt, name="OmpAgent", harness="omp")
    oc_agent = await _mk_agent(async_session, rt, name="OcAgent", harness="openclaude")

    flagged = await rp.mark_agents_for_recreate(async_session, "omp")

    await async_session.refresh(omp_agent)
    await async_session.refresh(oc_agent)
    assert flagged == 1
    assert omp_agent.pending_recreate is True
    assert oc_agent.pending_recreate is False


@pytest.mark.asyncio
async def test_mark_uses_derived_harness_for_legacy_null(async_session):
    """harness=NULL rows derive from the runtime (vllm_docker → openclaude)."""
    rt = await _mk_rt(async_session)
    legacy = await _mk_agent(async_session, rt, name="Legacy", harness=None)

    flagged = await rp.mark_agents_for_recreate(async_session, "openclaude")

    await async_session.refresh(legacy)
    assert flagged == 1
    assert legacy.pending_recreate is True


@pytest.mark.asyncio
async def test_mark_skips_host_and_wrong_harness(async_session):
    rt = await _mk_rt(async_session)
    host = await _mk_agent(async_session, rt, name="Host",
                           agent_runtime="host", harness="openclaude")
    other = await _mk_agent(async_session, rt, name="Other", harness="claude")

    flagged = await rp.mark_agents_for_recreate(async_session, "openclaude")

    await async_session.refresh(host)
    await async_session.refresh(other)
    assert flagged == 0
    assert host.pending_recreate is False
    assert other.pending_recreate is False


# ── recreate_pending_agents ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recreate_idle_forces_recreate_and_clears_flag(async_session, fake_redis):
    rt = await _mk_rt(async_session)
    agent = await _mk_agent(async_session, rt, harness="openclaude", pending=True)
    mock_restart = MagicMock(return_value={"status": "recreated"})

    with (
        patch.object(rp, "restart_docker_agent_container", mock_restart),
        patch.object(rp, "wait_for_agent_healthy",
                     new=AsyncMock(return_value={"healthy": True})) as mock_health,
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.recreate_pending_agents(async_session)

    # force_recreate=True must be threaded through to the recreate call.
    mock_restart.assert_called_once_with(agent, force_recreate=True)
    # vllm runtime → no omp ready-signal override.
    assert mock_health.await_args.kwargs["ready_signals"] is None
    await async_session.refresh(agent)
    assert agent.pending_recreate is False


@pytest.mark.asyncio
async def test_recreate_skips_busy_agents(async_session, fake_redis):
    rt = await _mk_rt(async_session)
    agent = await _mk_agent(async_session, rt, harness="openclaude",
                            busy=True, pending=True)
    mock_restart = MagicMock(return_value={"status": "recreated"})

    with (
        patch.object(rp, "restart_docker_agent_container", mock_restart),
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.recreate_pending_agents(async_session)

    mock_restart.assert_not_called()
    await async_session.refresh(agent)
    assert agent.pending_recreate is True  # stays flagged for next tick


@pytest.mark.asyncio
async def test_recreate_omp_passes_ready_signals(async_session, fake_redis):
    rt = await _mk_rt(async_session, slug="omp-rt", runtime_type="omp")
    agent = await _mk_agent(async_session, rt, name="OmpAgent",
                            harness="omp", pending=True)

    with (
        patch.object(rp, "restart_docker_agent_container",
                     MagicMock(return_value={"status": "recreated"})),
        patch.object(rp, "wait_for_agent_healthy",
                     new=AsyncMock(return_value={"healthy": True})) as mock_health,
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.recreate_pending_agents(async_session)

    assert mock_health.await_args.kwargs["ready_signals"] == rp._OMP_READY_SIGNALS


@pytest.mark.asyncio
async def test_recreate_circuit_breaker_gives_up(async_session, fake_redis):
    rt = await _mk_rt(async_session)
    agent = await _mk_agent(async_session, rt, harness="openclaude", pending=True)

    with (
        patch.object(rp, "restart_docker_agent_container",
                     MagicMock(return_value={"status": "error: boom"})),
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        for _ in range(rp.MAX_SYNC_ATTEMPTS):
            await rp.recreate_pending_agents(async_session)
            await async_session.refresh(agent)

    assert agent.pending_recreate is False  # breaker tripped — stop retrying


@pytest.mark.asyncio
async def test_recreate_skips_when_switch_lock_held(async_session, fake_redis):
    """No recreate / no failure bump while a runtime switch holds the lock —
    the agent stays flagged for the next tick."""
    rt = await _mk_rt(async_session)
    agent = await _mk_agent(async_session, rt, harness="openclaude", pending=True)

    await fake_redis.set(switch_mod._lock_key(agent.id), "1", nx=True, ex=120)
    mock_restart = MagicMock(return_value={"status": "recreated"})

    with (
        patch.object(rp, "restart_docker_agent_container", mock_restart),
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.recreate_pending_agents(async_session, force=True)

    mock_restart.assert_not_called()
    await async_session.refresh(agent)
    assert agent.pending_recreate is True
    fails = await fake_redis.get(rp.RedisKeys.agent_recreate_fails(str(agent.id)))
    assert fails is None
