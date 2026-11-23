"""GET /api/v1/files/recent — newest files first, straight from the index
(knowledge concept P3). Code checkouts (``workspaces``, not indexed) and
directories never show up; ``root`` narrows to one root."""

from __future__ import annotations

from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.file_index import FileIndexEntry
from tests.conftest import test_engine


async def _seed() -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add_all(
            [
                FileIndexEntry(root_key="vault", rel_path="old.md", name="old.md", mtime=100.0, size=1),
                FileIndexEntry(root_key="media", rel_path="new.png", name="new.png", mtime=500.0, size=2, mime="image/png"),
                FileIndexEntry(root_key="deliverables", rel_path="t/mid.pdf", name="mid.pdf", mtime=300.0, size=3),
                FileIndexEntry(root_key="vault", rel_path="sub", name="sub", is_directory=True, mtime=900.0),
                FileIndexEntry(root_key="workspaces", rel_path="repo/x.py", name="x.py", mtime=999.0),
                FileIndexEntry(root_key="storyboard-images", rel_path="a.png", name="a.png", mtime=998.0),
            ]
        )
        await s.commit()


async def test_recent_newest_first_without_dirs_and_workspaces(auth_client: AsyncClient):
    await _seed()
    resp = await auth_client.get("/api/v1/files/recent")
    assert resp.status_code == 200, resp.text
    names = [r["name"] for r in resp.json()["results"]]
    assert names == ["new.png", "mid.pdf", "old.md"], names
    first = resp.json()["results"][0]
    assert first["root"] == "media" and first["rel_path"] == "new.png" and first["mtime"] == 500.0


async def test_recent_limit_and_root_filter(auth_client: AsyncClient):
    await _seed()
    resp = await auth_client.get("/api/v1/files/recent", params={"limit": 1})
    assert [r["name"] for r in resp.json()["results"]] == ["new.png"]

    resp = await auth_client.get("/api/v1/files/recent", params={"root": "vault"})
    assert [r["name"] for r in resp.json()["results"]] == ["old.md"]

    # a huge limit is clamped, not an error
    resp = await auth_client.get("/api/v1/files/recent", params={"limit": 100000})
    assert resp.status_code == 200


async def test_recent_rejects_unknown_and_sensitive_root(auth_client: AsyncClient):
    for bad in ("secrets", "nope"):
        resp = await auth_client.get("/api/v1/files/recent", params={"root": bad})
        assert resp.status_code == 404, bad


async def test_recent_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/files/recent")
    assert resp.status_code == 401
