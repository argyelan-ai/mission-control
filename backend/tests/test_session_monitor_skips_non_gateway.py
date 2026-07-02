"""Heartbeat-Health-Check darf cli-bridge und host-Agents nicht als overdue markieren.

Phase 29 (Gateway-Sunset): die ehemaligen Tests fuer _check_agent_sessions
sind entfallen — die Methode existiert nicht mehr (sessions_list war
gateway-only). _check_heartbeat_health bleibt bestehen und filtert
weiterhin auf agent_runtime='openclaw'. Post Phase 30 (DB-Enum-Drop von
openclaw) wird der Check effektiv no-op.
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
# Legacy: vorhanden aber kein Gateway-Session
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
    """_check_heartbeat_health darf cli-bridge/host-Agents nicht auf overdue pruefen."""
    from app.services.watchdog.session_monitor import SessionMonitorMixin
    from app.models.agent import Agent
    from app.utils import utcnow
    from datetime import timedelta

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        _, docker_agent, _ = await _create_agents_with_mixed_runtimes(db)
        # Fake old heartbeat → würde overdue sein, aber der Check sollte sie skippen
        docker_agent.last_seen_at = utcnow() - timedelta(hours=1)
        db.add(docker_agent)
        await db.commit()

    monitor = SessionMonitorMixin()

    with patch("app.services.watchdog.session_monitor.emit_event", new_callable=AsyncMock) as mock_emit:
        async with AsyncSession(test_engine, expire_on_commit=False) as db:
            await monitor._check_heartbeat_health(db)

    # Kein agent.heartbeat_overdue Event fuer den docker-agent
    calls = [c for c in mock_emit.call_args_list if len(c.args) >= 2 and c.args[1] == "agent.heartbeat_overdue"]
    assert len(calls) == 0
