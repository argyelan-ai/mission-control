"""W3-C — POST /api/v1/agent/vault/note related_notes wikilinks.

Constraint relaxed 2026-05-15 (Operator): min_length=2 → 0. First note in a new
area legitimately has no neighbours, and the wikilink-backfill job connects
orphans retroactively via Qdrant similarity + Spark LLM. Tests now verify
that an empty / missing related_notes list is accepted, while max_length=8
remains enforced.

Auth pattern: same minimal-app approach as test_vault_audit_block.py.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Minimal test app ─────────────────────────────────────────────────────────


def _make_agent_vault_app() -> FastAPI:
    """Minimal FastAPI app with only the agent vault router."""
    from app.database import get_session
    from app.routers.vault import agent_router

    app = FastAPI()
    app.include_router(agent_router)

    # vault_index not used by the write route, but state must exist for read routes
    app.state.vault_index = MagicMock()

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    app.dependency_overrides[get_session] = override_get_session
    return app


# ── Fixture: agent with vault:write scope ────────────────────────────────────


@pytest.fixture
async def agent_auth(tmp_path: Path, monkeypatch):
    """Yields (AsyncClient, headers) authenticated as an agent with vault:write scope."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name="Constraint Test Agent",
            role="developer",
            agent_token_hash=token_hash,
            scopes=["vault:read", "vault:write"],
        )
        s.add(agent)
        await s.commit()

    # Point vault_path at a tmp dir so the inbox mkdir succeeds on the happy path
    monkeypatch.setattr("app.config.settings.vault_path", tmp_path / "vault")

    app = _make_agent_vault_app()
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_note_accepts_single_related(agent_auth: AsyncClient):
    """1 related_note is now accepted (previously rejected as <2)."""
    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "test note",
            "content": "x" * 200,
            "type": "knowledge",
            "tags": ["test"],
            "related_notes": ["[[only-one]]"],
        },
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_write_note_accepts_with_2_related(agent_auth: AsyncClient):
    """2 related_notes still works (the recommended pattern)."""
    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "test note two",
            "content": "x" * 200,
            "type": "knowledge",
            "tags": ["test"],
            "related_notes": ["[[note-a]]", "[[note-b]]"],
        },
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_write_note_title_required(agent_auth: AsyncClient):
    """Missing title still returns 422 (title is required, related_notes is not)."""
    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "content": "x" * 200,
            "type": "knowledge",
            "tags": ["test"],
            "related_notes": ["[[a]]", "[[b]]"],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_write_note_missing_related_notes_field_accepted(agent_auth: AsyncClient):
    """Omitting related_notes entirely now succeeds (defaults to empty list).

    First note in a new vault area legitimately has no neighbours. The
    wikilink-backfill job connects orphans retroactively.
    """
    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "first note in new area",
            "content": "x" * 200,
            "type": "knowledge",
            "tags": ["test"],
        },
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_write_note_empty_related_notes_accepted(agent_auth: AsyncClient):
    """Explicit empty related_notes list is accepted (same as omitting)."""
    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "explicit empty related",
            "content": "x" * 200,
            "type": "lesson",
            "tags": ["test"],
            "related_notes": [],
        },
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_write_note_rejects_more_than_8_related(agent_auth: AsyncClient):
    """max_length=8 stays enforced — prevents pathological link spam."""
    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "too many related",
            "content": "x" * 200,
            "type": "knowledge",
            "tags": ["test"],
            "related_notes": [f"[[note-{i}]]" for i in range(9)],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_write_note_accepts_with_3_related(agent_auth: AsyncClient):
    """W3-C: more than 2 related_notes is also accepted."""
    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "well connected note",
            "content": "y" * 150,
            "type": "lesson",
            "tags": ["integration"],
            "related_notes": ["[[note-a]]", "[[note-b]]", "[[note-c]]"],
        },
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_write_note_with_relations_field(agent_auth: AsyncClient):
    """W3-C: optional relations dict is persisted in envelope frontmatter."""
    import frontmatter

    monkeypatched_vault = None

    resp = await agent_auth.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "supersedes old note",
            "content": "Updated content " * 10,
            "type": "lesson",
            "tags": ["arch"],
            "related_notes": ["[[old-note]]", "[[context-note]]"],
            "relations": {"old-note": "supersedes"},
        },
    )
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert data.get("ok") is True
