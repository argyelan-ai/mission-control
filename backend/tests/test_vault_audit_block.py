"""W4.1 — Vault API rejects auto+journal notes at the API boundary.

Tests for the guard added to POST /api/v1/agent/vault/note in vault.py.

Auth pattern: create an Agent with generate_agent_token() + vault:write scope,
following the same approach as test_resolution_auto_promote.py.
The vault_index is stubbed with a MagicMock (write route doesn't use it).
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
async def agent_write_client(tmp_path: Path, monkeypatch):
    """AsyncClient authenticated as an agent with vault:write scope."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name="Vault Test Agent",
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
async def test_post_vault_note_rejects_journal_with_auto_tag(agent_write_client: AsyncClient):
    """W4.1: type=journal + tag 'auto' must return 422 with guidance message."""
    resp = await agent_write_client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "Task erledigt: some task",
            "content": "x" * 50,
            "type": "journal",
            "tags": ["auto", "task_done"],
            "related_notes": ["[[note-a]]", "[[note-b]]"],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.text.lower()
    assert "auto-reflection" in body or "task comment" in body


@pytest.mark.asyncio
async def test_post_vault_note_rejects_journal_with_auto_among_many_tags(
    agent_write_client: AsyncClient,
):
    """W4.1: 'auto' anywhere in tags list triggers the guard, regardless of other tags."""
    resp = await agent_write_client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "reflection fold",
            "content": "x" * 50,
            "type": "journal",
            "tags": ["auto", "reflection_fold", "projekt:foo"],
            "related_notes": ["[[note-a]]", "[[note-b]]"],
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_post_vault_note_accepts_journal_without_auto_tag(agent_write_client: AsyncClient):
    """W4.1: type=journal WITHOUT 'auto' tag must be accepted (200/201)."""
    resp = await agent_write_client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "weekly review 2026-W20",
            "content": "x" * 50,
            "type": "journal",
            "tags": ["weekly", "manual"],
            "related_notes": ["[[note-a]]", "[[note-b]]"],
        },
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_post_vault_note_accepts_non_journal_with_auto_tag(agent_write_client: AsyncClient):
    """W4.1: guard is AND — type != journal with 'auto' tag passes through."""
    resp = await agent_write_client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "auto lesson",
            "content": "x" * 50,
            "type": "lesson",
            "tags": ["auto", "task_done"],
            "related_notes": ["[[note-a]]", "[[note-b]]"],
        },
    )
    # lesson + auto is not blocked — guard is strictly journal AND auto
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_post_vault_note_accepts_note_type_with_no_tags(agent_write_client: AsyncClient):
    """W4.1: ordinary note with no tags is unaffected."""
    resp = await agent_write_client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "some knowledge",
            "content": "x" * 50,
            "type": "note",
            "tags": [],
            "related_notes": ["[[note-a]]", "[[note-b]]"],
        },
    )
    assert resp.status_code in (200, 201), resp.text
