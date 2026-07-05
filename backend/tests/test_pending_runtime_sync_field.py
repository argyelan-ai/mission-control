"""agents.pending_runtime_sync — model field default + persistence."""
import pytest
from app.models.agent import Agent


@pytest.mark.asyncio
async def test_pending_runtime_sync_defaults_false(async_session):
    agent = Agent(name="SyncTest", role="developer")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is False

    agent.pending_runtime_sync = True
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.pending_runtime_sync is True
