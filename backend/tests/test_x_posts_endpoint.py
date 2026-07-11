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


# ── media_paths ──────────────────────────────────────────────────────────────


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    """Redirect the media containment root to tmp_path so tests can create files."""
    from app.services import x_publisher

    monkeypatch.setattr(x_publisher, "MEDIA_ROOT", tmp_path)
    return tmp_path


def _touch(root, rel: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


@pytest.mark.asyncio
async def test_x_post_draft_with_media_stores_paths_in_payload(
    client: AsyncClient, fake_redis, media_root
):
    from app.models.approval import Approval

    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )
    img1 = _touch(media_root, "bench-1/shot-1.png")
    img2 = _touch(media_root, "bench-1/shot-2.png")

    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Side-by-side screenshots", "media_paths": [str(img1), str(img2)]},
    )
    assert resp.status_code == 201, resp.text
    approval_id = uuid.UUID(resp.json()["approval_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = await s.get(Approval, approval_id)
        assert approval.payload["media_paths"] == [str(img1), str(img2)]


@pytest.mark.asyncio
async def test_x_post_draft_without_media_has_empty_media_paths(
    client: AsyncClient, fake_redis
):
    from app.models.approval import Approval

    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Text-only draft, backward compatible"},
    )
    assert resp.status_code == 201, resp.text
    approval_id = uuid.UUID(resp.json()["approval_id"])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = await s.get(Approval, approval_id)
        assert approval.payload["media_paths"] == []


@pytest.mark.asyncio
async def test_x_post_draft_invalid_media_rejected_422_before_approval(
    client: AsyncClient, fake_redis, media_root
):
    from sqlmodel import select
    from app.models.approval import Approval

    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )
    video = _touch(media_root, "bench-1/grid.mp4")
    image = _touch(media_root, "bench-1/shot.png")

    # mixed video + image is never allowed
    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Mixed media", "media_paths": [str(video), str(image)]},
    )
    assert resp.status_code == 422, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(Approval).where(Approval.action_type == "x_post"))).all()
        assert rows == []


@pytest.mark.asyncio
async def test_x_post_draft_nonexistent_media_rejected_422(
    client: AsyncClient, fake_redis, media_root
):
    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )

    resp = await client.post(
        "/api/v1/agent/x-posts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Ghost file", "media_paths": [str(media_root / "nope.png")]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_x_post_draft_idempotency_includes_media_paths(
    client: AsyncClient, fake_redis, media_root
):
    board_id = await _make_board()
    agent, token = await _make_agent(
        name="Writer", scopes=["content:submit"], board_id=board_id
    )
    img = _touch(media_root, "bench-1/shot.png")
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post(
        "/api/v1/agent/x-posts",
        headers=headers,
        json={"text": "Same text", "media_paths": [str(img)]},
    )
    assert first.status_code == 201, first.text

    # same text, same media -> existing approval returned
    dup = await client.post(
        "/api/v1/agent/x-posts",
        headers=headers,
        json={"text": "Same text", "media_paths": [str(img)]},
    )
    assert dup.status_code == 200, dup.text
    assert dup.json()["approval_id"] == first.json()["approval_id"]

    # same text, NO media -> different draft -> new approval
    text_only = await client.post(
        "/api/v1/agent/x-posts", headers=headers, json={"text": "Same text"}
    )
    assert text_only.status_code == 201, text_only.text
    assert text_only.json()["approval_id"] != first.json()["approval_id"]
