"""Tests for the Phase E Task-Klammer.

Covers:
- frontmatter validate accepts optional `task` UUID and rejects junk
- VaultIndex stores + filters by `task` column
- POST /vault/note (admin) persists task field
- GET /vault/related/{task_id} returns all notes with that task
- Deliverable wrapper auto-sets task from deliverable.task_id (Phase A
  already did this; the test pins the behaviour against regressions)
"""

from __future__ import annotations

import uuid
from pathlib import Path

import frontmatter as fm_lib
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.helpers.vault_frontmatter import FrontmatterError, validate_frontmatter
from tests.conftest import test_engine


# ── Helper App ────────────────────────────────────────────────────────────────


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

    user_id = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        if not await s.get(User, user_id):
            s.add(User(id=user_id, email="te@mc.local", name="TE", role="admin", is_active=True))
            await s.commit()
    token = create_access_token(str(user_id), "admin")

    fa = _make_app(vault_index)
    transport = ASGITransport(app=fa)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


# ── Frontmatter validator ─────────────────────────────────────────────────────


def test_validate_accepts_valid_task_uuid():
    validate_frontmatter({
        "id": "x",
        "type": "note",
        "agent": "mark",
        "date": "2026-05-15T13:00:00+00:00",
        "task": "11111111-2222-3333-4444-555555555555",
    })  # must not raise


def test_validate_accepts_missing_task():
    """Old notes without `task` stay valid."""
    validate_frontmatter({
        "id": "x", "type": "note", "agent": "mark",
        "date": "2026-05-15T13:00:00+00:00",
    })


def test_validate_rejects_garbage_task():
    with pytest.raises(FrontmatterError, match="task"):
        validate_frontmatter({
            "id": "x", "type": "note", "agent": "mark",
            "date": "2026-05-15T13:00:00+00:00",
            "task": "not-a-uuid",
        })


# ── VaultIndex task column ────────────────────────────────────────────────────


def test_vault_index_stores_and_filters_by_task(vault_index, vault_path: Path):
    note_with = vault_path / "agents/x/notes/with.md"
    note_with.parent.mkdir(parents=True)
    task_id = "11111111-2222-3333-4444-555555555555"
    note_with.write_text(fm_lib.dumps(fm_lib.Post(
        "body",
        id="w", type="note", agent="x",
        date="2026-05-15T13:00:00+00:00",
        task=task_id,
    )))
    note_without = vault_path / "agents/x/notes/without.md"
    note_without.write_text(fm_lib.dumps(fm_lib.Post(
        "body",
        id="wo", type="note", agent="x",
        date="2026-05-15T13:00:00+00:00",
    )))

    vault_index.upsert(note_with, fm_lib.load(str(note_with)))
    vault_index.upsert(note_without, fm_lib.load(str(note_without)))

    filtered = list(vault_index.list_all(task=task_id))
    assert len(filtered) == 1
    assert filtered[0]["id"] == "w"

    all_rows = list(vault_index.list_all())
    assert len(all_rows) == 2


# ── POST /vault/note round-trip ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_note_persists_task_field(admin_client, vault_path):
    task_id = "11111111-2222-3333-4444-555555555555"
    r = await admin_client.post(
        "/api/v1/vault/note",
        json={
            "title": "Bound to a Task",
            "content": "Body content here",
            "task_id": task_id,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    full = vault_path / body["path"]
    post = fm_lib.load(str(full))
    assert post["task"] == task_id


@pytest.mark.asyncio
async def test_create_note_without_task_is_fine(admin_client, vault_path):
    r = await admin_client.post(
        "/api/v1/vault/note",
        json={"title": "Free Floating", "content": "x"},
    )
    assert r.status_code == 200, r.text
    full = vault_path / r.json()["path"]
    post = fm_lib.load(str(full))
    assert "task" not in post.metadata


# ── GET /vault/related/{task_id} ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_related_returns_notes_with_same_task(admin_client, vault_path):
    task_id = "11111111-2222-3333-4444-555555555555"
    # Create three notes — two with task, one without
    for title in ["First", "Second"]:
        r = await admin_client.post(
            "/api/v1/vault/note",
            json={"title": f"Hit {title}", "content": "x", "task_id": task_id},
        )
        assert r.status_code == 200, r.text
    r2 = await admin_client.post(
        "/api/v1/vault/note", json={"title": "Stray Note", "content": "x"},
    )
    assert r2.status_code == 200

    rel = await admin_client.get(f"/api/v1/vault/related/{task_id}")
    assert rel.status_code == 200, rel.text
    body = rel.json()
    assert body["task_id"] == task_id
    assert body["count"] == 2
    titles = {n["title"] for n in body["notes"]}
    assert titles == {"Hit First", "Hit Second"}


@pytest.mark.asyncio
async def test_related_rejects_non_uuid(admin_client):
    r = await admin_client.get("/api/v1/vault/related/not-a-uuid")
    assert r.status_code == 400
