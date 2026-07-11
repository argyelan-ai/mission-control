"""Live SOUL preview for the onboarding wizard (2026-07-10).

Step 2 of the wizard shows the operator the generated persona as they
type. This renders SOUL.md.j2 for a transient, non-persisted agent.
"""
import pytest


@pytest.mark.asyncio
async def test_preview_soul_renders_name(auth_client):
    resp = await auth_client.post(
        "/api/v1/agents/preview-soul",
        json={"name": "Aurora", "role": "developer", "emoji": "🌅"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "soul_md" in body
    assert "Aurora" in body["soul_md"]


@pytest.mark.asyncio
async def test_preview_soul_does_not_persist(auth_client, async_session):
    from sqlmodel import select
    from app.models.agent import Agent

    before = len((await async_session.exec(select(Agent))).all())
    await auth_client.post("/api/v1/agents/preview-soul", json={"name": "Ghost"})
    after = len((await async_session.exec(select(Agent))).all())
    assert before == after  # no agent row created


@pytest.mark.asyncio
async def test_preview_soul_requires_name(auth_client):
    resp = await auth_client.post("/api/v1/agents/preview-soul", json={})
    assert resp.status_code == 422
