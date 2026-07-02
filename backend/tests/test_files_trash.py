"""Tests for the trash list/restore/purge endpoints — containment + auth + re-index."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.deliverable import TaskDeliverable
from app.models.file_index import FileIndexEntry
from tests.conftest import test_engine


# ── auth client fixtures (mirror test_files_delete) ─────────────────────────


def _role_token(role: str):
    from app.auth import create_access_token

    user_id = uuid.uuid4()

    async def _seed_and_token():
        from app.models.user import User

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(
                User(
                    id=user_id,
                    email=f"{role}-trash@mc.local",
                    name=f"Test {role}",
                    role=role,
                    is_active=True,
                )
            )
            await s.commit()
        return create_access_token(str(user_id), role)

    return _seed_and_token


@pytest.fixture
async def operator_client(client: AsyncClient) -> AsyncClient:
    token = await _role_token("operator")()
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
async def viewer_client(client: AsyncClient) -> AsyncClient:
    token = await _role_token("viewer")()
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ── helpers ─────────────────────────────────────────────────────────────────


def _mk_root(tmp_path, key: str = "deliverables"):
    base = tmp_path / ".mc" / key
    base.mkdir(parents=True, exist_ok=True)
    return base


def _seed_trash(tmp_path, ts, root_key, rel, content=b"x"):
    p = tmp_path / ".mc" / ".trash" / ts / root_key / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


async def _count(model, **where) -> int:
    from sqlalchemy import func
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        stmt = select(func.count()).select_from(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return (await s.exec(stmt)).one()


# ── list ─────────────────────────────────────────────────────────────────────


async def test_list_trash_parses_entries(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_trash(tmp_path, "20260618-120000", "deliverables", "a/b.txt", b"hello")

    resp = await operator_client.get("/api/v1/files/trash")
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert e["trash_id"] == "20260618-120000/deliverables/a/b.txt"
    assert e["original_root"] == "deliverables"
    assert e["original_subpath"] == "a/b.txt"
    assert e["name"] == "b.txt"
    assert e["deleted_at"] == "2026-06-18T12:00:00"


async def test_list_trash_empty(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    resp = await operator_client.get("/api/v1/files/trash")
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


# ── restore ──────────────────────────────────────────────────────────────────


async def test_restore_happy_path_reappears_in_list(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _mk_root(tmp_path)
    _seed_trash(tmp_path, "20260618-120000", "deliverables", "back.txt", b"bytes")

    resp = await operator_client.post(
        "/api/v1/files/trash/restore",
        json={"trash_ids": ["20260618-120000/deliverables/back.txt"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["restored"] == [
        {"trash_id": "20260618-120000/deliverables/back.txt", "root": "deliverables", "subpath": "back.txt"}
    ]
    assert data["skipped"] == []
    # file is back under its original root
    assert (tmp_path / ".mc" / "deliverables" / "back.txt").read_bytes() == b"bytes"
    # and removed from .trash
    assert not (tmp_path / ".mc" / ".trash" / "20260618-120000" / "deliverables" / "back.txt").exists()

    # re-indexed → appears in /search + /list
    s = await operator_client.get("/api/v1/files/search", params={"q": "back"})
    assert "back.txt" in {x["name"] for x in s.json()["results"]}
    lst = await operator_client.get("/api/v1/files/list", params={"root": "deliverables"})
    assert "back.txt" in {x["name"] for x in lst.json()["entries"]}


async def test_restore_escape_rejected_nothing_moved(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    # plant an outside file that a forged ../ id might target
    outside = tmp_path / ".mc" / "deliverables" / "precious.txt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"PRECIOUS")

    resp = await operator_client.post(
        "/api/v1/files/trash/restore",
        json={"trash_ids": ["../../etc/passwd"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored"] == []
    assert data["skipped"] == [{"trash_id": "../../etc/passwd", "reason": "escape"}]
    assert outside.read_bytes() == b"PRECIOUS"  # untouched


async def test_restore_unknown_root_skipped(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_trash(tmp_path, "20260618-120000", "bogus", "x.txt")
    resp = await operator_client.post(
        "/api/v1/files/trash/restore",
        json={"trash_ids": ["20260618-120000/bogus/x.txt"]},
    )
    assert resp.status_code == 200
    assert resp.json()["skipped"] == [
        {"trash_id": "20260618-120000/bogus/x.txt", "reason": "unknown_root"}
    ]
    # .trash file stays
    assert (tmp_path / ".mc" / ".trash" / "20260618-120000" / "bogus" / "x.txt").exists()


async def test_restore_blocked_root_skipped(operator_client, tmp_path, monkeypatch):
    """A trash_id pointing at a non-deletable root → skipped, nothing written out."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    for root in ("vault", "attachments", "workspaces", "shared-deliverables"):
        _seed_trash(tmp_path, "20260618-120000", root, "x.txt")
        resp = await operator_client.post(
            "/api/v1/files/trash/restore",
            json={"trash_ids": [f"20260618-120000/{root}/x.txt"]},
        )
        assert resp.status_code == 200, root
        assert resp.json()["skipped"] == [
            {"trash_id": f"20260618-120000/{root}/x.txt", "reason": "blocked_root"}
        ], root
        # nothing written outside .trash
        assert not (tmp_path / ".mc" / root / "x.txt").exists()


async def test_restore_uniquifies_no_overwrite(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "c.txt").write_bytes(b"PREEXISTING")
    _seed_trash(tmp_path, "20260618-120000", "deliverables", "c.txt", b"FROMTRASH")

    resp = await operator_client.post(
        "/api/v1/files/trash/restore",
        json={"trash_ids": ["20260618-120000/deliverables/c.txt"]},
    )
    assert resp.status_code == 200, resp.text
    sub = resp.json()["restored"][0]["subpath"]
    assert sub != "c.txt" and sub.startswith("c.txt-")
    assert (base / "c.txt").read_bytes() == b"PREEXISTING"  # NOT overwritten
    assert (base / sub).read_bytes() == b"FROMTRASH"


async def test_restore_cascade_deleted_deliverable_limitation(operator_client, tmp_path, monkeypatch):
    """v1 limitation: restore brings the file back with a fresh file_index row
    (deliverable_id=None) and does NOT recreate a TaskDeliverable."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _mk_root(tmp_path)
    _seed_trash(tmp_path, "20260618-120000", "deliverables", "rep.txt", b"r")
    pre_deliv = await _count(TaskDeliverable)

    resp = await operator_client.post(
        "/api/v1/files/trash/restore",
        json={"trash_ids": ["20260618-120000/deliverables/rep.txt"]},
    )
    assert resp.status_code == 200, resp.text
    # no new TaskDeliverable row
    assert await _count(TaskDeliverable) == pre_deliv
    # a file_index row exists with deliverable_id None
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = (
            await s.exec(
                select(FileIndexEntry).where(
                    FileIndexEntry.root_key == "deliverables",
                    FileIndexEntry.rel_path == "rep.txt",
                )
            )
        ).first()
        assert row is not None
        assert row.deliverable_id is None


async def test_delete_then_restore_end_to_end(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "round.txt").write_text("trip")

    # delete → vanishes from disk into .trash
    d = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["round.txt"]}
    )
    assert d.status_code == 200, d.text
    assert not (base / "round.txt").exists()

    # the trash list now surfaces it
    lst = await operator_client.get("/api/v1/files/trash")
    ids = [e["trash_id"] for e in lst.json()["entries"] if e["name"] == "round.txt"]
    assert len(ids) == 1

    # restore → reappears on disk
    r = await operator_client.post("/api/v1/files/trash/restore", json={"trash_ids": ids})
    assert r.status_code == 200, r.text
    assert r.json()["restored"][0]["subpath"] == "round.txt"
    assert (base / "round.txt").exists()


# ── purge ────────────────────────────────────────────────────────────────────


async def test_purge_happy_path(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    p = _seed_trash(tmp_path, "20260618-120000", "deliverables", "only.txt")

    resp = await operator_client.post(
        "/api/v1/files/trash/purge",
        json={"trash_ids": ["20260618-120000/deliverables/only.txt"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == ["20260618-120000/deliverables/only.txt"]
    assert not p.exists()


async def test_purge_escape_rejected(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    outside = tmp_path / ".mc" / "deliverables" / "keepme.txt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"KEEP")

    resp = await operator_client.post(
        "/api/v1/files/trash/purge",
        json={"trash_ids": ["../deliverables/keepme.txt"]},
    )
    assert resp.status_code == 200
    assert resp.json()["purged"] == []
    assert resp.json()["skipped"] == [{"trash_id": "../deliverables/keepme.txt", "reason": "escape"}]
    assert outside.exists() and outside.read_bytes() == b"KEEP"  # untouched


async def test_purge_empty_parent_cleanup(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _seed_trash(tmp_path, "20260618-120000", "deliverables", "gone.txt")
    _seed_trash(tmp_path, "20260101-000000", "deliverables", "keep.txt")

    resp = await operator_client.post(
        "/api/v1/files/trash/purge",
        json={"trash_ids": ["20260618-120000/deliverables/gone.txt"]},
    )
    assert resp.status_code == 200, resp.text
    assert not (tmp_path / ".mc" / ".trash" / "20260618-120000").exists()
    # sibling ts with files survives
    assert (tmp_path / ".mc" / ".trash" / "20260101-000000" / "deliverables" / "keep.txt").exists()


# ── auth (mirror test_files_delete) ──────────────────────────────────────────


async def test_auth_all_three_endpoints(client, tmp_path, monkeypatch):
    from app.auth import create_access_token
    from app.models.user import User

    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _mk_root(tmp_path)
    _seed_trash(tmp_path, "20260618-120000", "deliverables", "x.txt")

    tokens: dict[str, str] = {}
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for role in ("viewer", "operator", "admin"):
            uid = uuid.uuid4()
            s.add(User(id=uid, email=f"{role}-3ep@mc.local", name=role, role=role, is_active=True))
            tokens[role] = create_access_token(str(uid), role)
        await s.commit()

    calls = [
        ("GET", "/api/v1/files/trash", None),
        ("POST", "/api/v1/files/trash/restore", {"trash_ids": []}),
        ("POST", "/api/v1/files/trash/purge", {"trash_ids": []}),
    ]
    for method, url, body in calls:
        # unauth → 401
        r = await client.request(method, url, json=body)
        assert r.status_code == 401, (method, url)
        # viewer → 403
        r = await client.request(
            method, url, json=body, headers={"Authorization": f"Bearer {tokens['viewer']}"}
        )
        assert r.status_code == 403, (method, url)
        # operator → 200
        r = await client.request(
            method, url, json=body, headers={"Authorization": f"Bearer {tokens['operator']}"}
        )
        assert r.status_code == 200, (method, url, r.text)
        # admin → 200
        r = await client.request(
            method, url, json=body, headers={"Authorization": f"Bearer {tokens['admin']}"}
        )
        assert r.status_code == 200, (method, url, r.text)


async def test_max_batch_422(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    ids = [f"20260618-120000/deliverables/f{i}.txt" for i in range(201)]
    for url in ("/api/v1/files/trash/restore", "/api/v1/files/trash/purge"):
        r = await operator_client.post(url, json={"trash_ids": ids})
        assert r.status_code == 422, url
