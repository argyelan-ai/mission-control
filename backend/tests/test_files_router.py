"""Tests for the global Files router (/api/v1/files)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.config import settings


def _seed_vault(tmp_path):
    vault = tmp_path / ".mc" / "vault"
    sub = vault / "sub"
    sub.mkdir(parents=True)
    (vault / "note.md").write_text("# Hello MC")
    (sub / "deep.txt").write_text("deep")
    return vault


async def test_roots_excludes_sensitive(auth_client: AsyncClient):
    resp = await auth_client.get("/api/v1/files/roots")
    assert resp.status_code == 200
    keys = {r["key"] for r in resp.json()["roots"]}
    assert "vault" in keys
    assert "deliverables" in keys
    for sensitive in ("secrets", "agents", "logs", "backups", "browser-profiles"):
        assert sensitive not in keys


async def test_roots_expose_deletable_flag(auth_client: AsyncClient):
    """The delete-gating UI depends on a per-root `deletable` boolean in /roots."""
    resp = await auth_client.get("/api/v1/files/roots")
    assert resp.status_code == 200
    roots = {r["key"]: r for r in resp.json()["roots"]}
    assert all("deletable" in r for r in roots.values()), "every root must carry deletable"
    for k in ("deliverables", "media", "shared-artifacts", "mcp-screenshots", "storyboard-images"):
        assert roots[k]["deletable"] is True, f"{k} must be deletable"
    for k in ("vault", "workspaces", "attachments", "shared-deliverables"):
        assert roots[k]["deletable"] is False, f"{k} must NOT be deletable"


async def test_unauthenticated_rejected(client: AsyncClient):
    resp = await client.get("/api/v1/files/roots")
    assert resp.status_code == 401


async def test_list_lists_files(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get("/api/v1/files/list", params={"root": "vault", "subpath": ""})
    assert resp.status_code == 200
    data = resp.json()
    names = {e["name"] for e in data["entries"]}
    assert names == {"sub", "note.md"}
    # dirs first
    assert data["entries"][0]["name"] == "sub"
    assert data["entries"][0]["is_directory"] is True


async def test_list_traversal_rejected(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get(
        "/api/v1/files/list", params={"root": "vault", "subpath": "../../etc"}
    )
    assert resp.status_code == 400


async def test_list_sensitive_root_404(auth_client: AsyncClient):
    for bad in ("secrets", "agents", "does-not-exist"):
        resp = await auth_client.get("/api/v1/files/list", params={"root": bad, "subpath": ""})
        assert resp.status_code == 404, bad


async def test_content_download_sets_attachment(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get(
        "/api/v1/files/content",
        params={"root": "vault", "subpath": "note.md", "download": "true"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-disposition", "").startswith("attachment")
    assert resp.content == b"# Hello MC"


async def test_content_inline_no_attachment(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    resp = await auth_client.get(
        "/api/v1/files/content", params={"root": "vault", "subpath": "note.md"}
    )
    assert resp.status_code == 200
    assert "attachment" not in resp.headers.get("content-disposition", "")


async def test_meta_native_open_hidden_when_unreachable(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)

    async def _unreachable():
        return False

    monkeypatch.setattr("app.routers.files._native_open_reachable", _unreachable)
    resp = await auth_client.get(
        "/api/v1/files/meta", params={"root": "vault", "subpath": "note.md"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["native_open_available"] is False
    assert data["reachable"] is True
    assert data["is_directory"] is False


async def test_search_finds_indexed_file(auth_client: AsyncClient, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_vault(tmp_path)
    # populate the index
    r = await auth_client.post("/api/v1/files/reindex")
    assert r.status_code == 200
    resp = await auth_client.get("/api/v1/files/search", params={"q": "note", "root": "vault"})
    assert resp.status_code == 200
    names = {x["name"] for x in resp.json()["results"]}
    assert "note.md" in names


async def test_open_container_only_root_409(auth_client: AsyncClient):
    # shared-deliverables (Docker named volume) has no host path → can't reveal
    resp = await auth_client.post(
        "/api/v1/files/open", json={"root": "shared-deliverables", "subpath": "x", "reveal": True}
    )
    assert resp.status_code in (404, 409)  # 404 if path missing, 409 container-only
