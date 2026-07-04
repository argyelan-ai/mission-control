"""Switch progress breadcrumbs (ADR-054) — publish helper + GET route."""
import json
import uuid

import pytest

from app.redis_client import RedisKeys, get_redis
from app.services.agent_runtime_switch import publish_switch_progress


@pytest.mark.asyncio
async def test_publish_and_read_progress(async_session, auth_client):
    from app.models.agent import Agent
    agent = Agent(name="ProgressTest", role="developer")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    await publish_switch_progress(agent.id, "restarting")
    redis = await get_redis()
    raw = await redis.get(RedisKeys.agent_switch_progress(str(agent.id)))
    assert json.loads(raw)["step"] == "restarting"

    resp = await auth_client.get(
        f"/api/v1/agents/{agent.id}/runtime-switch-progress"
    )
    assert resp.status_code == 200
    assert resp.json()["step"] == "restarting"


@pytest.mark.asyncio
async def test_progress_route_empty_state(async_session, auth_client):
    from app.models.agent import Agent
    agent = Agent(name="NoProgress", role="developer")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    resp = await auth_client.get(
        f"/api/v1/agents/{agent.id}/runtime-switch-progress"
    )
    assert resp.status_code == 200
    assert resp.json() == {"step": None}
