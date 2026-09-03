"""GET /api/v1/internal/bootstrap must require the shared bootstrap secret
when one is configured.

Regression guard for PR #404 (Rex review, HIGH finding): the endpoint used
to hand out every agent's vault secrets (MC_AGENT_TOKEN, GH_TOKEN, provider
keys) to anyone who could reach it and guess an agent name — no auth header,
no working Caddy exception. See routers/internal.py::_check_bootstrap_secret
and config.py::validate_boot_secrets (production hard-fails without the
secret configured).
"""
import uuid

import pytest
from httpx import AsyncClient

import app.config as app_config
from app.models.agent import Agent
from tests.conftest import test_engine
from sqlmodel.ext.asyncio.session import AsyncSession

BOOTSTRAP_SECRET = "test-internal-bootstrap-secret"


@pytest.fixture
def with_bootstrap_secret():
    """Configure INTERNAL_BOOTSTRAP_SECRET for the duration of one test."""
    original = app_config.settings.internal_bootstrap_secret
    app_config.settings.internal_bootstrap_secret = BOOTSTRAP_SECRET
    try:
        yield BOOTSTRAP_SECRET
    finally:
        app_config.settings.internal_bootstrap_secret = original


@pytest.mark.asyncio
async def test_bootstrap_rejects_missing_header_when_secret_configured(
    client: AsyncClient, with_bootstrap_secret
):
    resp = await client.get(
        "/api/v1/internal/bootstrap?agent_name=__nonexistent__"
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_bootstrap_rejects_wrong_secret(client: AsyncClient, with_bootstrap_secret):
    resp = await client.get(
        "/api/v1/internal/bootstrap?agent_name=__nonexistent__",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_bootstrap_accepts_correct_secret(
    client: AsyncClient, with_bootstrap_secret
):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Freecode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
        )
        s.add(agent)
        await s.commit()

    resp = await client.get(
        f"/api/v1/internal/bootstrap?agent_name={agent.name}",
        headers={"Authorization": f"Bearer {with_bootstrap_secret}"},
    )
    # 200 (tokens found) or 404 ("no tokens for agent") — both mean the auth
    # check let it through and business logic ran, which is what this test
    # verifies. 401 would mean the auth check itself failed.
    assert resp.status_code in (200, 404), resp.text


@pytest.mark.asyncio
async def test_bootstrap_open_when_secret_unconfigured(client: AsyncClient):
    # Default test settings leave internal_bootstrap_secret empty — local
    # dev/test must keep working exactly like before this fix.
    assert app_config.settings.internal_bootstrap_secret == ""
    resp = await client.get(
        "/api/v1/internal/bootstrap?agent_name=__nonexistent__"
    )
    assert resp.status_code == 404, resp.text
