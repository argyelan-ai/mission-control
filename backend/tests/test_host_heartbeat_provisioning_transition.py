"""Host-agent provision_status flips to 'provisioned' on first heartbeat
(2026-07-10 E2E, Lauf 3 finding — Fix E).

The generic host provisioning chain (Fix C/D) got a host agent all the way
to a working poller sending real heartbeats — last_seen_at was set — but
provision_status stayed stuck on 'provisioning' forever, since nothing
transitions it once staging (autoload disabled, the normal case) leaves it
there. Result: /health-check's `ready` flag stayed false even though the
agent was demonstrably alive and heartbeating.

cli-bridge agents don't need this: their provisioning flow
(services/provisioning.py) already flips to 'provisioned' itself once the
container starts — the heartbeat endpoint must NOT touch their status.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_agent(session: AsyncSession, *, agent_runtime: str, provision_status: str):
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name="Poller Host",
        agent_runtime=agent_runtime,
        provision_status=provision_status,
        agent_token_hash=token_hash,
        scopes=["heartbeat", "tasks:read"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


@pytest.mark.asyncio
async def test_host_agent_first_heartbeat_flips_provisioning_to_provisioned(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, agent_runtime="host", provision_status="provisioning")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.provision_status == "provisioned"
    assert fresh.last_seen_at is not None
    assert fresh.provisioned_at is not None


@pytest.mark.asyncio
async def test_host_agent_second_heartbeat_does_not_change_already_provisioned(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, agent_runtime="host", provision_status="provisioning")

    first = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        after_first = await s.get(Agent, agent.id)
    first_provisioned_at = after_first.provisioned_at
    assert after_first.provision_status == "provisioned"

    second = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        after_second = await s.get(Agent, agent.id)
    assert after_second.provision_status == "provisioned"
    assert after_second.provisioned_at == first_provisioned_at


@pytest.mark.asyncio
async def test_cli_bridge_agent_heartbeat_leaves_provision_status_untouched(client: AsyncClient):
    """cli-bridge already flips to 'provisioned' in its own provisioning flow
    (services/provisioning.py) — the heartbeat endpoint must not race or
    override that for a different runtime."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, agent_runtime="cli-bridge", provision_status="local")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.provision_status == "local"
    assert fresh.last_seen_at is not None


@pytest.mark.asyncio
async def test_host_agent_heartbeat_while_already_provisioned_untouched(client: AsyncClient):
    """A host agent that loaded via launchctl (autoload enabled) is already
    'provisioned' at first heartbeat — must stay that way, no duplicate
    provisioned_at bump or event on every single heartbeat."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, agent_runtime="host", provision_status="provisioned")

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)
    assert fresh.provision_status == "provisioned"
