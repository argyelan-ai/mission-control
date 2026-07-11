"""Prompt Library (Benchmark Studio Baustein 3, core): model + CRUD API tests.

Fixture pattern mirrors tests/test_reference_files.py: `session` / `auth_client`
from conftest (SQLite in-memory + JWT admin user).
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.prompt_template import PromptTemplate


# ── Model ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_template_model_defaults(session: AsyncSession):
    tpl = PromptTemplate(
        title="Spinning cube",
        body="Build a spinning 3D cube in a single self-contained index.html.",
    )
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)

    assert isinstance(tpl.id, uuid.UUID)
    assert tpl.tags == []          # JSON default list, never None
    assert tpl.created_at is not None
    assert tpl.updated_at is not None


# ── CRUD API ───────────────────────────────────────────────────────────────


async def _create(auth_client: AsyncClient, title: str, body: str = "Body", tags: list[str] | None = None) -> dict:
    r = await auth_client.post(
        "/api/v1/prompt-templates",
        json={"title": title, "body": body, "tags": tags or []},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(auth_client: AsyncClient):
    created = await _create(
        auth_client, "Spinning cube", "Build a spinning 3D cube.", ["3d", "animation"]
    )
    assert created["title"] == "Spinning cube"
    assert created["tags"] == ["3d", "animation"]

    r = await auth_client.get(f"/api/v1/prompt-templates/{created['id']}")
    assert r.status_code == 200
    assert r.json()["body"] == "Build a spinning 3D cube."


@pytest.mark.asyncio
async def test_list_with_q_and_tag_filters(auth_client: AsyncClient):
    await _create(auth_client, "Spinning cube", tags=["3d"])
    await _create(auth_client, "Mini game: snake", tags=["games"])
    await _create(auth_client, "Landing page hero", tags=["web", "3d"])

    # no filter → all 3
    r = await auth_client.get("/api/v1/prompt-templates")
    assert r.status_code == 200
    assert len(r.json()) == 3

    # ?q= case-insensitive title search
    r = await auth_client.get("/api/v1/prompt-templates?q=CUBE")
    titles = [t["title"] for t in r.json()]
    assert titles == ["Spinning cube"]

    # ?tag= exact membership
    r = await auth_client.get("/api/v1/prompt-templates?tag=3d")
    titles = {t["title"] for t in r.json()}
    assert titles == {"Spinning cube", "Landing page hero"}

    # combined
    r = await auth_client.get("/api/v1/prompt-templates?q=cube&tag=games")
    assert r.json() == []


@pytest.mark.asyncio
async def test_patch_partial_update(auth_client: AsyncClient):
    created = await _create(auth_client, "Old title", "Old body", ["a"])

    r = await auth_client.patch(
        f"/api/v1/prompt-templates/{created['id']}",
        json={"tags": ["b", "c"]},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["tags"] == ["b", "c"]
    assert data["title"] == "Old title"   # untouched fields survive
    assert data["body"] == "Old body"

    r2 = await auth_client.patch(
        f"/api/v1/prompt-templates/{created['id']}",
        json={"title": "New title"},
    )
    assert r2.json()["title"] == "New title"


@pytest.mark.asyncio
async def test_delete_then_404(auth_client: AsyncClient):
    created = await _create(auth_client, "To delete")

    r = await auth_client.delete(f"/api/v1/prompt-templates/{created['id']}")
    assert r.status_code == 204

    r2 = await auth_client.get(f"/api/v1/prompt-templates/{created['id']}")
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_unknown_id_is_404(auth_client: AsyncClient):
    missing = uuid.uuid4()
    for method, kwargs in (
        ("get", {}),
        ("patch", {"json": {"title": "x"}}),
        ("delete", {}),
    ):
        r = await getattr(auth_client, method)(f"/api/v1/prompt-templates/{missing}", **kwargs)
        assert r.status_code == 404, f"{method} → {r.status_code}"


@pytest.mark.asyncio
async def test_validation_422(auth_client: AsyncClient):
    # empty title
    r = await auth_client.post("/api/v1/prompt-templates", json={"title": "", "body": "x"})
    assert r.status_code == 422
    # missing body
    r = await auth_client.post("/api/v1/prompt-templates", json={"title": "x"})
    assert r.status_code == 422
    # title too long (max 200)
    r = await auth_client.post("/api/v1/prompt-templates", json={"title": "x" * 201, "body": "y"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_requires_user_auth(client: AsyncClient):
    """User JWT required (require_user) — bare client gets 401."""
    r = await client.get("/api/v1/prompt-templates")
    assert r.status_code == 401
