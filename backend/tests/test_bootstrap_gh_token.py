"""Bootstrap endpoint delivers GH_TOKEN from vault.

Regression guard: the /api/v1/internal/bootstrap response must include
GH_TOKEN when a vault secret with key='github_token' is present. Agent
containers rely on this to run `gh auth login --with-token` in their
entrypoint so they can push commits autonomously.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.secret import Secret
from app.services.encryption import encrypt
from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_bootstrap_returns_gh_token_when_secret_present(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Freecode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
        )
        s.add(agent)
        s.add(Secret(
            key="github_token",
            encrypted_value=encrypt("gho_test_token_abcdef"),
            provider="github",
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/internal/bootstrap?agent_name={agent.name}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("GH_TOKEN") == "gho_test_token_abcdef"


@pytest.mark.asyncio
async def test_bootstrap_omits_gh_token_when_secret_absent(client: AsyncClient):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Freecode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
        )
        s.add(agent)
        # Need at least one token in response so bootstrap doesn't 404
        s.add(Secret(
            key="ollama_api_key",
            encrypted_value=encrypt("k-test"),
            provider="ollama",
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/internal/bootstrap?agent_name={agent.name}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "GH_TOKEN" not in body


@pytest.mark.asyncio
async def test_bootstrap_gh_token_key_is_exact(client: AsyncClient):
    """Other secret keys (github_token_backup, etc.) must not leak in.
    Only the canonical 'github_token' key is delivered.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Freecode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
        )
        s.add(agent)
        s.add(Secret(
            key="github_token_backup",
            encrypted_value=encrypt("gho_wrong_token"),
            provider="github",
        ))
        s.add(Secret(
            key="ollama_api_key",
            encrypted_value=encrypt("k-test"),
            provider="ollama",
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/internal/bootstrap?agent_name={agent.name}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "GH_TOKEN" not in body


@pytest.mark.asyncio
async def test_bootstrap_includes_context_max(client: AsyncClient):
    """CTX-01 (Plan 06-02): bootstrap response includes CONTEXT_MAX from
    agent.context_max so the container entrypoint can export it as fallback
    denominator for poll.sh ctx% scrapes (Plan 06-03 consumes this)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Freecode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
            context_max=180_000,
        )
        s.add(agent)
        # Seed a token so bootstrap doesn't 404 on empty tokens dict.
        s.add(Secret(
            key="ollama_api_key",
            encrypted_value=encrypt("k-test"),
            provider="ollama",
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/internal/bootstrap?agent_name={agent.name}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "CONTEXT_MAX" in body
    assert body["CONTEXT_MAX"] == "180000"


@pytest.mark.asyncio
async def test_bootstrap_context_max_falls_back_to_200000_when_zero(client: AsyncClient):
    """When agent.context_max is 0 (or falsy), CONTEXT_MAX falls back to
    "200000" — claude-sonnet-4-6 default per CONTEXT.md D-03."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"Freecode-{uuid.uuid4().hex[:6]}",
            role="developer",
            agent_runtime="cli-bridge",
            context_max=0,
        )
        s.add(agent)
        s.add(Secret(
            key="ollama_api_key",
            encrypted_value=encrypt("k-test"),
            provider="ollama",
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/internal/bootstrap?agent_name={agent.name}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["CONTEXT_MAX"] == "200000"
