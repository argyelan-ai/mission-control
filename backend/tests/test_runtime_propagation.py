"""Runtime propagation (ADR-054) — idle sync, busy flagging, circuit breaker."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
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
    ):
        for _ in range(rp.MAX_SYNC_ATTEMPTS):
            await rp.sync_pending_agents(async_session)
            await async_session.refresh(agent)

    assert agent.pending_runtime_sync is False  # breaker tripped — stop retrying
