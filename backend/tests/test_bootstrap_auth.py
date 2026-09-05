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
    # PR #404 Review, NIEDRIG-2: das war frueher "200 oder 404" und bewies
    # damit nur, dass der Auth-Check nicht 401 warf — nicht, dass ueberhaupt
    # etwas ausgeliefert wird. Der Endpunkt setzt AGENT_RECYCLER_ENABLED und
    # CONTEXT_MAX immer, ein existierender Agent muss also 200 mit Inhalt
    # liefern (404 hiesse: Agent nicht gefunden oder gar keine Tokens).
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict) and body, resp.text
    assert "CONTEXT_MAX" in body, (
        f"Bootstrap lieferte kein CONTEXT_MAX — Payload: {sorted(body)}"
    )


@pytest.mark.asyncio
async def test_bootstrap_open_when_secret_unconfigured(client: AsyncClient):
    # Default test settings leave internal_bootstrap_secret empty — local
    # dev/test must keep working exactly like before this fix.
    assert app_config.settings.internal_bootstrap_secret == ""
    resp = await client.get(
        "/api/v1/internal/bootstrap?agent_name=__nonexistent__"
    )
    assert resp.status_code == 404, resp.text
