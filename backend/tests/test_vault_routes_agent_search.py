"""Tests for GET /api/v1/agent/vault/search (M.3 T6 — fills M.2 T7 gap).

Covers the agent-scoped search route that was fixed in M.2 T7 (current_agent.slug
bug replaced with slugify(current_agent.name)). Zero tests existed before this file.

Pattern follows test_vault_routes_write.py:
- Minimal FastAPI app with vault agent_router
- Real agent + real PBKDF2 token
- app.state.vault_index stubbed with a MagicMock for unit-level isolation
- AsyncClient with Bearer token
"""

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── App factory ───────────────────────────────────────────────────────────────


def _make_search_app(vault_index) -> FastAPI:
    """Minimal FastAPI app with vault agent_router + DB session override."""
    from app.database import get_session
    from app.routers.vault import agent_router

    fa = FastAPI()
    fa.include_router(agent_router)
    fa.state.vault_index = vault_index

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    fa.dependency_overrides[get_session] = override_get_session
    return fa


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_stub_index(hits=None):
    """Return a MagicMock vault_index whose .search() returns the given hits list."""
    stub = MagicMock()
    stub.search.return_value = hits if hits is not None else []
    return stub


async def _make_agent_client(name: str, scopes: list[str]):
    """Create an agent with the given scopes and return (raw_token, agent_id)."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw_token, token_hash = generate_agent_token()
    agent_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=agent_id,
            name=name,
            role="developer",
            agent_token_hash=token_hash,
            scopes=scopes,
        )
        s.add(agent)
        await s.commit()

    return raw_token, agent_id


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_search_uses_agent_auth_returns_hits():
    """Authenticated agent with vault:read scope gets 200 + correct hits count."""
    from app.scopes import Scope

    stub_hits = [
        {"id": "aaa", "path": "agents/sparky/lessons/a.md", "agent": "sparky"},
        {"id": "bbb", "path": "agents/cody/lessons/b.md", "agent": "cody"},
    ]
    vault_index = _make_stub_index(stub_hits)

    raw_token, _ = await _make_agent_client(
        name="Sparky",
        scopes=[Scope.VAULT_READ.value],
    )

    app_instance = _make_search_app(vault_index)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/search?q=foo")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["q"] == "foo"
    assert len(data["hits"]) == 2
    assert data["requesting_agent"] == "sparky"


@pytest.mark.asyncio
async def test_agent_search_requires_vault_read_scope():
    """Agent without vault:read scope receives 403."""
    from app.scopes import Scope

    vault_index = _make_stub_index([])

    # Give a real non-vault scope so backward-compat (empty=ALL) is NOT triggered.
    raw_token, _ = await _make_agent_client(
        name="ScopelesAgent",
        scopes=[Scope.HEARTBEAT.value],
    )

    app_instance = _make_search_app(vault_index)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/search?q=foo")

    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_agent_search_passes_agent_param_through_to_index():
    """?agent=sparky is forwarded verbatim to index.search(agent='sparky')."""
    from app.scopes import Scope

    vault_index = _make_stub_index([])

    raw_token, _ = await _make_agent_client(
        name="Cody",
        scopes=[Scope.VAULT_READ.value],
    )

    app_instance = _make_search_app(vault_index)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/search?q=foo&agent=sparky")

    assert r.status_code == 200, r.text
    # Verify the stub was called with the correct agent keyword argument
    vault_index.search.assert_called_once()
    call_kwargs = vault_index.search.call_args
    assert call_kwargs.kwargs.get("agent") == "sparky" or (
        # positional fallback: search(q, agent, type, limit)
        len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "sparky"
    )


@pytest.mark.asyncio
async def test_agent_search_includes_requesting_agent_slug():
    """requesting_agent field is the slugified agent name (M.2 T7 carry-over fix).

    Agent named 'Test Agent Name' → slug must be 'test-agent-name'.
    This verifies the fix: current_agent.name (not .slug) is used in the route,
    then passed through slugify().
    """
    from app.scopes import Scope

    vault_index = _make_stub_index([])

    raw_token, _ = await _make_agent_client(
        name="Test Agent Name",
        scopes=[Scope.VAULT_READ.value],
    )

    app_instance = _make_search_app(vault_index)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/search?q=x")

    assert r.status_code == 200, r.text
    data = r.json()
    # slugify("Test Agent Name") → "test-agent-name"
    assert data["requesting_agent"] == "test-agent-name"
