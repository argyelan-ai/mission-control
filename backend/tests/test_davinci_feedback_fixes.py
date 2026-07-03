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
    """Davinci called /api/v1/storyboards/{id} with an agent token, got a
    401 'Invalid token' → had to guess that it should be
    /api/v1/agent/storyboards/{id}. Backend now gives a clear hint."""
    # Core route instead of vertical route (storyboards): the hint mechanism
    # lives in auth.py (core); vertical routes don't exist in the public
    # export -> the test hit 404 there (public CI finding 2026-07-02).
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
    """Token that is neither JWT nor agent-shape → generic 401, no
    false-positive agent hint."""
    r = await client.get(
        "/api/v1/boards",
        headers={"Authorization": "Bearer this-is-not-a-token"},
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "agent-scoped" not in detail.lower()


@pytest.mark.asyncio
async def test_short_hex_token_keeps_generic_message(client: AsyncClient):
    """Shorter hex token (32 chars) → no agent hint (agent tokens are
    always 64 hex chars)."""
    r = await client.get(
        "/api/v1/boards",
        headers={"Authorization": f"Bearer {'a' * 32}"},
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "agent-scoped" not in detail.lower()
