"""Tests for vault REST API routes.

These tests create a minimal FastAPI test app that includes the vault router
directly, bypassing the main.py lifespan (which is T11's job). This lets us
verify route logic independently before T11 wires things into the full app.

Auth adaptations vs plan template:
- Plan used `client: TestClient` + `jwt_admin: str` — this codebase uses
  `AsyncClient` (async pattern consistent with all other test files).
- `vault_path` fixture is local to this module (not in conftest) for isolation.
- JWT admin token created inline following auth_client conftest pattern.

Note: test_get_vault_note_returns_frontmatter_and_content patches
app.config.settings.vault_path to point to the tmp vault.
"""

import uuid
from pathlib import Path

import frontmatter
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import test_engine


# ── Test app factory ─────────────────────────────────────────────────────────


def _make_vault_app(vault_index) -> FastAPI:
    """Minimal FastAPI app for vault route tests.

    Includes only the vault router + required auth + dependency overrides.
    """
    from app.database import get_session
    from app.routers.vault import router as vault_router
    from app.routers.vault import agent_router

    app = FastAPI()
    app.include_router(vault_router)
    app.include_router(agent_router)

    # Inject vault_index into app.state (normally done by T11 lifespan wiring)
    app.state.vault_index = vault_index

    # Override DB session to use test SQLite engine
    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    app.dependency_overrides[get_session] = override_get_session
    return app


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    """Temporary vault root for these route tests."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


@pytest.fixture
def vault_index(vault_path: Path):
    """Real VaultIndex backed by SQLite in the tmp vault."""
    from app.services.vault_index import VaultIndex

    db_path = vault_path / ".mc_index.db"
    idx = VaultIndex(db_path=db_path, vault_path=vault_path)
    yield idx
    idx.close()


@pytest.fixture
async def vault_client(vault_index, vault_path):
    """AsyncClient against the minimal vault test app, with JWT admin auth."""
    from app.auth import create_access_token
    from app.models.user import User

    app = _make_vault_app(vault_index)

    # Create admin user in test DB
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        existing = await s.get(User, user_id)
        if not existing:
            user = User(
                id=user_id,
                email="vaulttest@mc.local",
                name="Vault Test Admin",
                role="admin",
                is_active=True,
            )
            s.add(user)
            await s.commit()

    token = create_access_token(str(user_id), "admin")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac


@pytest.fixture
async def unauth_client(vault_index):
    """AsyncClient with no auth token."""
    app = _make_vault_app(vault_index)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Helper ────────────────────────────────────────────────────────────────────


def _seed_vault(vault: Path) -> None:
    """Create two test notes for routing tests."""
    a = vault / "agents" / "sparky" / "lessons" / "a.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "rate limiting xai",
                id="aaa",
                type="lesson",
                agent="sparky",
                date="2026-05-14T15:00:00Z",
                tags=["api", "xai"],
            )
        )
    )
    b = vault / "agents" / "cody" / "lessons" / "b.md"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "react component patterns",
                id="bbb",
                type="lesson",
                agent="cody",
                date="2026-05-14T15:00:00Z",
                tags=["frontend"],
            )
        )
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_get_vault_notes_list(vault_client: AsyncClient, vault_path: Path):
    """GET /api/v1/vault/notes returns all indexed notes after rebuild."""
    _seed_vault(vault_path)

    # Trigger rebuild (M.1 has no auto-rebuild on startup)
    r = await vault_client.post("/api/v1/vault/_admin/rebuild")
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["indexed"] == 2

    r = await vault_client.get("/api/v1/vault/notes")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    paths = {n["path"] for n in data["notes"]}
    assert "agents/sparky/lessons/a.md" in paths
    assert "agents/cody/lessons/b.md" in paths


async def test_get_vault_search(vault_client: AsyncClient, vault_path: Path):
    """GET /api/v1/vault/search?q= returns FTS5 hits."""
    _seed_vault(vault_path)
    r = await vault_client.post("/api/v1/vault/_admin/rebuild")
    assert r.status_code == 200, r.text

    r = await vault_client.get("/api/v1/vault/search?q=xai")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["q"] == "xai"
    assert len(data["hits"]) == 1
    assert data["hits"][0]["agent"] == "sparky"


async def test_get_vault_note_returns_frontmatter_and_content(
    vault_client: AsyncClient, vault_path: Path
):
    """GET /api/v1/vault/note/{path} returns frontmatter dict + content string."""
    _seed_vault(vault_path)

    # Patch settings.vault_path so the route reads from our tmp vault
    import app.config

    original_vault_path = app.config.settings.vault_path
    app.config.settings.vault_path = vault_path
    try:
        r = await vault_client.get(
            "/api/v1/vault/note/agents%2Fsparky%2Flessons%2Fa.md"
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["frontmatter"]["type"] == "lesson"
        assert "rate limiting" in data["content"]
    finally:
        app.config.settings.vault_path = original_vault_path


async def test_path_traversal_in_note_rejected(vault_client: AsyncClient):
    """GET /api/v1/vault/note/../../etc/passwd returns 400."""
    r = await vault_client.get("/api/v1/vault/note/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 400, r.text


async def test_unauth_returns_401(unauth_client: AsyncClient):
    """GET /api/v1/vault/notes without auth token returns 401 or 403."""
    r = await unauth_client.get("/api/v1/vault/notes")
    assert r.status_code in (401, 403), r.text


async def test_track_view_records_view(vault_client: AsyncClient, vault_index):
    """POST /api/v1/vault/track-view records a view via vault_activity."""
    activity = MagicMock()
    activity.track_view = AsyncMock()

    # Inject stub into app state via the transport's app
    transport = vault_client._transport  # type: ignore[attr-defined]
    transport.app.state.vault_activity = activity

    r = await vault_client.post(
        "/api/v1/vault/track-view",
        json={"path": "agents/sparky/lessons/x.md"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    activity.track_view.assert_called_once_with(
        "agents/sparky/lessons/x.md", user_id="mark"
    )


async def test_track_view_rejects_path_traversal(vault_client: AsyncClient):
    """POST /api/v1/vault/track-view rejects paths with '..' (Pydantic 422)."""
    r = await vault_client.post(
        "/api/v1/vault/track-view",
        json={"path": "../etc/passwd"},
    )
    assert r.status_code == 422, r.text


async def test_track_view_returns_error_when_activity_not_initialized(
    vault_client: AsyncClient, vault_index
):
    """POST /api/v1/vault/track-view returns ok=False when vault_activity is None."""
    transport = vault_client._transport  # type: ignore[attr-defined]
    transport.app.state.vault_activity = None

    r = await vault_client.post(
        "/api/v1/vault/track-view",
        json={"path": "agents/sparky/lessons/x.md"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "error": "activity not initialized"}
