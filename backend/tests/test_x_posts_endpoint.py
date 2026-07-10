"""
Tests for POST /api/v1/agent/x-posts

Covers:
- Happy path: creates pending Approval (201)
- Draft validation: >280 chars rejected (400) before an Approval is created
- Idempotency: identical pending draft returns same approval_id (200)
- Scope enforcement: content:submit required (403 without it)
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent(*, name: str, scopes: list[str], board_id: uuid.UUID):
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            name=name,
            role="writer",
            scopes=scopes,
            board_id=board_id,
            agent_token_hash=token_hash,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

    return agent, raw_token


async def _make_board(name: str = "MC Dev") -> uuid.UUID:
    from app.models.board import Board

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name=name, slug=f"mc-dev-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)
        return board.id


@pytest.mark.asyncio
async def test_x_post_draft_happy_path(client: AsyncClient, fake_redis):
    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "MC now posts to X. Draft -> Approve -> Post."},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["existing"] is False
    uuid.UUID(body["approval_id"])


@pytest.mark.asyncio
async def test_x_post_draft_link_warning(client: AsyncClient, fake_redis):
    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Check this out: https://example.com/big-news"},
    )
    assert resp.status_code == 201, resp.text
    assert any("Kosten" in w for w in resp.json()["warnings"])


@pytest.mark.asyncio
async def test_x_post_draft_too_long_rejected(client: AsyncClient, fake_redis):
    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "x" * 281},
    )
    assert resp.status_code == 422, resp.text  # Pydantic max_length rejects first


@pytest.mark.asyncio
async def test_x_post_draft_idempotent(client: AsyncClient, fake_redis):
    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )
    draft = {"text": "Same draft, submitted twice."}

    first = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json=draft,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json=draft,
    )
    assert second.status_code == 200, second.text
    assert second.json()["existing"] is True
    assert second.json()["approval_id"] == first.json()["approval_id"]


@pytest.mark.asyncio
async def test_x_post_draft_requires_content_submit_scope(client: AsyncClient, fake_redis):
    # NOTE: an empty scopes list means "ALL_SCOPES" (backward-compat default in
    # get_agent_effective_scopes) — must pass a non-empty list that excludes
    # content:submit to actually exercise the 403 path.
    board_id = await _make_board()
    agent, token = await _make_agent(
        name="NoScope", scopes=["tasks:read"], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Should not be allowed"},
    )
    assert resp.status_code == 403
