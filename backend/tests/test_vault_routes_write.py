"""Tests for POST /api/v1/agent/vault/note write endpoint (M.2 T7).

Pattern follows test_vault_routes.py:
- Minimal FastAPI app with vault agent_router
- Real agent + real PBKDF2 token (same as test_clarification_callback_runtime_agnostic.py)
- monkeypatch settings.vault_path to tmp_path
- AsyncClient with Bearer token
"""

import uuid
from pathlib import Path

import frontmatter
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from tests.conftest import test_engine


# ── App factory ───────────────────────────────────────────────────────────────


def _make_write_app(vault_index) -> FastAPI:
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


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


@pytest.fixture
def vault_index(vault_path: Path):
    from app.services.vault_index import VaultIndex

    db_path = vault_path / ".mc_index.db"
    idx = VaultIndex(db_path=db_path, vault_path=vault_path)
    yield idx
    idx.close()


@pytest.fixture
async def agent_write_client(vault_index, vault_path, monkeypatch):
    """AsyncClient authenticated as a real agent with vault:write scope."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.scopes import Scope

    monkeypatch.setattr(app.config.settings, "vault_path", vault_path)

    raw_token, token_hash = generate_agent_token()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name="Sparky",
            role="developer",
            agent_token_hash=token_hash,
            scopes=[Scope.VAULT_WRITE.value, Scope.VAULT_READ.value],
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)

    app_instance = _make_write_app(vault_index)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac, vault_path


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_note_creates_envelope_in_inbox(agent_write_client):
    """POST /api/v1/agent/vault/note creates an envelope file in _inbox/."""
    client, vault_path = agent_write_client

    r = await client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "Rate limit on xAI",
            "content": "Observed: 429 above 10 req/s\nLesson: exponential backoff",
            "type": "lesson",
            "tags": ["api", "xai"],
            "related_notes": ["[[api-best-practices]]", "[[xai-integration]]"],
        },
    )

    assert r.status_code in (200, 201), r.text
    data = r.json()
    assert data["ok"] is True
    assert "envelope" in data
    assert "expected_target" in data

    # _inbox/ dir must exist with exactly one file
    inbox = vault_path / "_inbox"
    assert inbox.exists(), "_inbox directory not created"
    files = list(inbox.glob("*.md"))
    assert len(files) == 1, f"Expected 1 envelope, got {len(files)}"

    # Verify frontmatter fields
    post = frontmatter.load(str(files[0]))
    assert post.metadata["op"] == "upsert"
    assert post.metadata["type"] == "lesson"
    assert post.metadata["agent"] == "sparky"
    assert "id" in post.metadata
    assert "sha256" in post.metadata
    assert post.metadata["target"].startswith("agents/sparky/lessons/")


@pytest.mark.asyncio
async def test_post_note_with_explicit_target(agent_write_client):
    """When target is provided explicitly it is respected verbatim."""
    client, vault_path = agent_write_client

    r = await client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "Auth Migration",
            "content": "Lesson body",
            "type": "lesson",
            "target": "global/lessons/auth-migration.md",
            "related_notes": ["[[auth-overview]]", "[[migration-guide]]"],
        },
    )

    assert r.status_code in (200, 201), r.text
    data = r.json()
    assert data["expected_target"] == "global/lessons/auth-migration.md"

    # Envelope frontmatter must also carry the explicit target
    inbox = vault_path / "_inbox"
    files = list(inbox.glob("*.md"))
    assert len(files) == 1
    post = frontmatter.load(str(files[0]))
    assert post.metadata["target"] == "global/lessons/auth-migration.md"


@pytest.mark.asyncio
async def test_post_note_includes_idempotency_key(agent_write_client):
    """idempotency_key is persisted in the envelope frontmatter."""
    client, vault_path = agent_write_client

    r = await client.post(
        "/api/v1/agent/vault/note",
        json={
            "title": "Test idempotency",
            "content": "body content here",
            "type": "note",
            "idempotency_key": "test-key-123",
            "related_notes": ["[[note-a]]", "[[note-b]]"],
        },
    )

    assert r.status_code in (200, 201), r.text

    inbox = vault_path / "_inbox"
    files = list(inbox.glob("*.md"))
    assert len(files) == 1
    post = frontmatter.load(str(files[0]))
    assert post.metadata["idempotency_key"] == "test-key-123"


@pytest.mark.asyncio
async def test_post_note_without_auth_returns_401(vault_index, vault_path, monkeypatch):
    """Unauthenticated POST returns 401."""
    monkeypatch.setattr(app.config.settings, "vault_path", vault_path)

    app_instance = _make_write_app(vault_index)
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/api/v1/agent/vault/note",
            json={"title": "T", "content": "C"},
        )
    assert r.status_code in (401, 403), r.text


@pytest.mark.asyncio
async def test_post_note_rejects_path_traversal(agent_write_client):
    """target field must reject path traversal and absolute paths (422)."""
    client, _vault_path = agent_write_client

    bad_targets = [
        "../../etc/passwd",
        "/absolute/path",
        "global/../etc/x",
        "\\windows\\path",
    ]
    for bad_target in bad_targets:
        r = await client.post(
            "/api/v1/agent/vault/note",
            json={
                "title": "Test",
                "content": "body",
                "type": "note",
                "target": bad_target,
            },
        )
        assert r.status_code == 422, (
            f"Should reject {bad_target!r}, got {r.status_code}: {r.text}"
        )
