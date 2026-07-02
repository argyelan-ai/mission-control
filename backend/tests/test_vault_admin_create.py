"""Tests for POST /api/v1/vault/note (admin create note) — the UI 'Neuer
Eintrag' endpoint added so the operator can write vault entries from the memory
page. Different from POST /api/v1/agent/vault/note (envelope-via-inbox);
this endpoint writes the canonical file directly + re-indexes synchronously.
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


def _make_app(vault_index) -> FastAPI:
    from app.database import get_session
    from app.routers.vault import router as vault_router

    fa = FastAPI()
    fa.include_router(vault_router)
    fa.state.vault_index = vault_index

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    fa.dependency_overrides[get_session] = override_get_session
    return fa


@pytest.fixture
def vault_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "vault"
    p.mkdir()
    # Monkeypatch at the lowest-level fixture so EVERY test using vault_path
    # (and its dependents vault_index + admin_client) writes to tmp_path —
    # not the real ~/.mc/vault. Previously the monkeypatch was inside the
    # admin_client fixture, which meant VaultIndex was instantiated against
    # the prod vault first. See REVIEW note 2026-05-17.
    monkeypatch.setattr(app.config.settings, "vault_path", p)
    return p


@pytest.fixture
def vault_index(vault_path: Path):
    from app.services.vault_index import VaultIndex

    idx = VaultIndex(db_path=vault_path / ".mc_index.db", vault_path=vault_path)
    yield idx
    idx.close()


@pytest.fixture
async def admin_client(vault_index, vault_path):
    from app.auth import create_access_token
    from app.models.user import User

    user_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        if not await s.get(User, user_id):
            s.add(User(id=user_id, email="vca@mc.local", name="VCA", role="admin", is_active=True))
            await s.commit()

    token = create_access_token(str(user_id), "admin")
    headers = {"Authorization": f"Bearer {token}"}

    fa = _make_app(vault_index)
    transport = ASGITransport(app=fa)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac, vault_path


@pytest.mark.asyncio
async def test_create_note_writes_canonical_file(admin_client):
    client, vault_path = admin_client
    r = await client.post(
        "/api/v1/vault/note",
        json={
            "title": "Operator Test Eintrag",
            "content": "Body content here",
            "type": "note",
            "tags": ["personal", "test"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["path"].startswith("agents/mark/notes/")
    assert body["path"].endswith(".md")

    # File on disk has the expected frontmatter
    full = vault_path / body["path"]
    assert full.exists()
    post = frontmatter.load(str(full))
    assert post["title"] == "Operator Test Eintrag"
    assert post["agent"] == "mark"
    assert post["type"] == "note"
    assert set(post["tags"]) == {"personal", "test"}
    assert post.content == "Body content here"


@pytest.mark.asyncio
async def test_create_note_dedupes_and_strips_tags(admin_client):
    client, vault_path = admin_client
    r = await client.post(
        "/api/v1/vault/note",
        json={
            "title": "Tags Cleanup",
            "content": "x",
            "tags": ["foo", "#foo", " bar ", "", "bar"],
        },
    )
    assert r.status_code == 200, r.text
    full = vault_path / r.json()["path"]
    post = frontmatter.load(str(full))
    assert post["tags"] == ["foo", "bar"]


@pytest.mark.asyncio
async def test_create_note_rejects_invalid_type(admin_client):
    client, _ = admin_client
    r = await client.post(
        "/api/v1/vault/note",
        json={"title": "bad", "content": "x", "type": "totally_made_up"},
    )
    assert r.status_code == 422
    assert "type must be one of" in r.text


@pytest.mark.asyncio
async def test_create_note_indexes_synchronously(admin_client, vault_index):
    """After create, the note must already be returned by GET /vault/notes
    without an external rebuild — the response handler upserts on the spot."""
    client, _ = admin_client
    r = await client.post(
        "/api/v1/vault/note",
        json={"title": "Indexed Immediately", "content": "body"},
    )
    assert r.status_code == 200, r.text
    target = r.json()["path"]

    rows = list(vault_index.list_all())
    paths = {row["path"] for row in rows}
    assert target in paths


@pytest.mark.asyncio
async def test_create_note_custom_agent_namespace(admin_client):
    client, vault_path = admin_client
    r = await client.post(
        "/api/v1/vault/note",
        json={"title": "Other Bucket", "content": "x", "agent": "diary"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["path"].startswith("agents/diary/notes/")
