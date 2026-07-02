"""Tests for the Davinci self-reflection fixes (cf319ff1 2026-05-10).

Covers:
1. Agent-token shaped tokens (64 hex chars) hitting user-only routes
   get a precise hint pointing to /api/v1/agent/* instead of generic 401.
2. Non-hex tokens still get the generic message (no false-positive hint).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_agent_token_on_user_route_returns_helpful_hint(client: AsyncClient):
    """Davinci hat /api/v1/storyboards/{id} mit Agent-Token aufgerufen,
    bekam 401 'Invalid token' → musste raten dass es /api/v1/agent/storyboards/{id}
    sein muss. Backend gibt jetzt klaren Hinweis."""
    # Kernroute statt Vertical-Route (storyboards): der Hint-Mechanismus lebt
    # in auth.py (core); im Public-Export existieren Vertical-Routen nicht ->
    # der Test lief dort auf 404 (Public-CI-Fund 2026-07-02).
    fake_agent_token = "a" * 64  # 64-char hex = agent-token shape
    r = await client.get(
        "/api/v1/boards",
        headers={"Authorization": f"Bearer {fake_agent_token}"},
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "/api/v1/agent/boards" in detail
    assert "agent-scoped" in detail.lower()


@pytest.mark.asyncio
async def test_non_hex_token_keeps_generic_message(client: AsyncClient):
    """Token der weder JWT noch agent-shape ist → generischer 401, kein
    false-positive Agent-Hinweis."""
    r = await client.get(
        "/api/v1/boards",
        headers={"Authorization": "Bearer this-is-not-a-token"},
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "agent-scoped" not in detail.lower()


@pytest.mark.asyncio
async def test_short_hex_token_keeps_generic_message(client: AsyncClient):
    """Kürzeres hex-Token (32 chars) → kein Agent-Hint (Agent-Tokens sind
    immer 64 hex chars)."""
    r = await client.get(
        "/api/v1/boards",
        headers={"Authorization": f"Bearer {'a' * 32}"},
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "agent-scoped" not in detail.lower()
