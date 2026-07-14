import pytest
from app.models.agent import Agent


@pytest.mark.asyncio
async def test_agent_archived_at_defaults_none(session):
    agent = Agent(name="ArchiveFieldTest", agent_runtime="cli-bridge")
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    assert agent.archived_at is None


@pytest.mark.asyncio
async def test_agent_archived_at_can_be_set(session):
    from app.utils import utcnow
    agent = Agent(name="ArchiveFieldTest2", agent_runtime="cli-bridge")
    session.add(agent)
    await session.commit()
    agent.archived_at = utcnow()
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    assert agent.archived_at is not None
