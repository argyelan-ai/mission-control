"""The heartbeat health check must not mark cli-bridge and host agents as overdue.

Phase 29 (gateway sunset): the former tests for _check_agent_sessions
were dropped — the method no longer exists (sessions_list was
gateway-only). _check_heartbeat_health remains and still filters
on agent_runtime='openclaw'. Post Phase 30 (DB enum drop of
openclaw) the check effectively becomes a no-op.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_agents_with_mixed_runtimes(session: AsyncSession):
    from app.models.agent import Agent

    gateway_agent = Agent(
        id=uuid.uuid4(),
        name="Henry",
        agent_runtime="openclaw",
        status="idle",
        is_board_lead=True,
    )
    docker_agent = Agent(
        id=uuid.uuid4(),
        name="FreeCode",
        agent_runtime="cli-bridge",
# Legacy: present but no gateway session
        status="idle",
    )
    host_agent = Agent(
        id=uuid.uuid4(),
        name="Boss",
        agent_runtime="host",
        status="idle",
    )
    for a in (gateway_agent, docker_agent, host_agent):
        session.add(a)
    await session.commit()
    for a in (gateway_agent, docker_agent, host_agent):
        await session.refresh(a)
    return gateway_agent, docker_agent, host_agent


@pytest.mark.asyncio
async def test_heartbeat_health_skips_non_gateway_agents():
    """_check_heartbeat_health must not check cli-bridge/host agents for overdue."""
    from app.services.watchdog.session_monitor import SessionMonitorMixin
    from app.models.agent import Agent
    from app.utils import utcnow
    from datetime import timedelta

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        _, docker_agent, _ = await _create_agents_with_mixed_runtimes(db)
        # Fake old heartbeat → would be overdue, but the check should skip it
        docker_agent.last_seen_at = utcnow() - timedelta(hours=1)
        db.add(docker_agent)
        await db.commit()

    monitor = SessionMonitorMixin()

    with patch("app.services.watchdog.session_monitor.emit_event", new_callable=AsyncMock) as mock_emit:
        async with AsyncSession(test_engine, expire_on_commit=False) as db:
            await monitor._check_heartbeat_health(db)

    # No agent.heartbeat_overdue event for the docker agent
    calls = [c for c in mock_emit.call_args_list if len(c.args) >= 2 and c.args[1] == "agent.heartbeat_overdue"]
    assert len(calls) == 0
