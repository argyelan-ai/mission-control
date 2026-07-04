"""Runtime propagation (ADR-054) — idle sync, busy flagging, circuit breaker."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services import agent_runtime_switch as switch_mod
from app.services import runtime_propagation as rp
from app.services import sse as sse_mod


async def _mk_rt(session, *, slug="prop-rt", model="new-model"):
    rt = Runtime(
        slug=slug, display_name=slug, runtime_type="vllm_docker",
        endpoint="http://spark:8000/v1", model_identifier=model, enabled=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def _mk_agent(session, rt, *, name="Sparky", agent_runtime="cli-bridge",
                    busy=False, pending=False):
    agent = Agent(name=name, role="developer", agent_runtime=agent_runtime,
                  runtime_id=rt.id, pending_runtime_sync=pending)
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




@pytest.mark.asyncio
async def test_mark_agents_flags_cli_bridge_only(async_session):
    rt = await _mk_rt(async_session)
    cli = await _mk_agent(async_session, rt, name="CliAgent")
    host = await _mk_agent(async_session, rt, name="HostAgent", agent_runtime="host")

    flagged = await rp.mark_agents_for_sync(async_session, rt)

    await async_session.refresh(cli)
    await async_session.refresh(host)
    assert flagged == 1
    assert cli.pending_runtime_sync is True
    assert host.pending_runtime_sync is False


@pytest.mark.asyncio
async def test_sync_skips_busy_agents(async_session, fake_redis):
    rt = await _mk_rt(async_session)
    agent = await _mk_agent(async_session, rt, busy=True, pending=True)

    with (
        patch.object(rp, "sync_docker_agent_files", new=AsyncMock()) as mock_sync,
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.sync_pending_agents(async_session)

    mock_sync.assert_not_awaited()
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is True  # stays flagged for next tick


@pytest.mark.asyncio
async def test_sync_success_clears_flag_and_updates_model(async_session, fake_redis):
    rt = await _mk_rt(async_session, model="brand-new-model")
    agent = await _mk_agent(async_session, rt, pending=True)

    with (
        patch.object(rp, "sync_docker_agent_files", new=AsyncMock()),
        patch.object(rp, "restart_docker_agent_container",
                     return_value={"status": "restarted"}),
        patch.object(rp, "wait_for_agent_healthy",
                     new=AsyncMock(return_value={"healthy": True})),
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.sync_pending_agents(async_session)

    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False
    assert agent.model == "brand-new-model"


@pytest.mark.asyncio
async def test_sync_circuit_breaker_gives_up_after_max_attempts(async_session, fake_redis):
    rt = await _mk_rt(async_session)
    agent = await _mk_agent(async_session, rt, pending=True)

    with (
        patch.object(rp, "sync_docker_agent_files", new=AsyncMock()),
        patch.object(rp, "restart_docker_agent_container",
                     return_value={"status": "error: boom"}),
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        for _ in range(rp.MAX_SYNC_ATTEMPTS):
            await rp.sync_pending_agents(async_session)
            await async_session.refresh(agent)

    assert agent.pending_runtime_sync is False  # breaker tripped — stop retrying


@pytest.mark.asyncio
async def test_sync_scoped_to_runtime_id(async_session, fake_redis):
    """sync_pending_agents(runtime_id=X) only touches agents bound to X."""
    rt_a = await _mk_rt(async_session, slug="prop-rt-a")
    rt_b = await _mk_rt(async_session, slug="prop-rt-b")
    agent_a = await _mk_agent(async_session, rt_a, name="AgentA", pending=True)
    agent_b = await _mk_agent(async_session, rt_b, name="AgentB", pending=True)

    with (
        patch.object(rp, "sync_docker_agent_files", new=AsyncMock()),
        patch.object(rp, "restart_docker_agent_container",
                     return_value={"status": "restarted"}),
        patch.object(rp, "wait_for_agent_healthy",
                     new=AsyncMock(return_value={"healthy": True})),
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.sync_pending_agents(async_session, force=True, runtime_id=rt_a.id)

    await async_session.refresh(agent_a)
    await async_session.refresh(agent_b)
    assert agent_a.pending_runtime_sync is False  # synced
    assert agent_b.pending_runtime_sync is True  # untouched — different runtime


@pytest.mark.asyncio
async def test_sync_one_skips_when_switch_lock_held(async_session, fake_redis):
    """_sync_one must not restart / bump the failure counter while a manual
    runtime switch holds the mc:agent:{id}:runtime-switch lock — it should
    just skip this tick and leave the agent flagged for the next one."""
    rt = await _mk_rt(async_session)
    agent = await _mk_agent(async_session, rt, pending=True)

    # Simulate agent_runtime_switch.switch_agent_runtime() holding its lock.
    await fake_redis.set(switch_mod._lock_key(agent.id), "1", nx=True, ex=120)

    with (
        patch.object(rp, "sync_docker_agent_files", new=AsyncMock()) as mock_sync_files,
        patch.object(rp, "restart_docker_agent_container") as mock_restart,
        patch.object(rp, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(sse_mod, "get_redis", _fake_get_redis(fake_redis)),
        patch.object(switch_mod, "get_redis", _fake_get_redis(fake_redis)),
    ):
        await rp.sync_pending_agents(async_session, force=True)

    mock_sync_files.assert_not_awaited()
    mock_restart.assert_not_called()
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is True  # stays flagged, no failure bumped
    fails = await fake_redis.get(rp.RedisKeys.agent_model_sync_fails(str(agent.id)))
    assert fails is None
