import uuid
import pytest


@pytest.mark.asyncio
async def test_archive_endpoint_sets_flag(auth_client, make_agent):
    agent = await make_agent(name="Dev", agent_runtime="cli-bridge")
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None


@pytest.mark.asyncio
async def test_archive_busy_agent_returns_409(auth_client, make_agent):
    agent = await make_agent(name="Busy", agent_runtime="cli-bridge",
                             current_task_id=uuid.uuid4())
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/archive")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_restore_endpoint_clears_flag(auth_client, make_agent):
    from app.utils import utcnow
    agent = await make_agent(name="Dev", agent_runtime="cli-bridge", archived_at=utcnow())
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/restore")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is None


@pytest.mark.asyncio
async def test_archive_missing_agent_404(auth_client):
    resp = await auth_client.post(f"/api/v1/agents/{uuid.uuid4()}/archive")
    assert resp.status_code == 404
