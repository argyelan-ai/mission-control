import pytest
from sqlmodel import select

from app.models.agent import Agent


@pytest.mark.asyncio
async def test_agent_harness_defaults_to_none(async_session):
    agent = Agent(name="harness-test-agent", agent_runtime="cli-bridge")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.harness is None


@pytest.mark.asyncio
async def test_agent_harness_persists_value(async_session):
    agent = Agent(name="harness-test-agent2", agent_runtime="cli-bridge", harness="omp")
    async_session.add(agent)
    await async_session.commit()
    row = (
        await async_session.execute(select(Agent).where(Agent.name == "harness-test-agent2"))
    ).scalar_one()
    assert row.harness == "omp"
