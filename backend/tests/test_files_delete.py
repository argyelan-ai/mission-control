"""Tests for POST /api/v1/files/delete — safe soft-delete + cascade + auth."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.deliverable import TaskDeliverable
from app.models.deliverable_reference import DeliverableReference
from app.models.file_index import FileIndexEntry

# conftest.test_engine — direct DB seeding into the same SQLite as the app.
from tests.conftest import test_engine


# ── auth client fixtures (conftest auth_client is admin) ───────────────────


def _role_client(client: AsyncClient, role: str) -> AsyncClient:
    from app.auth import create_access_token

    user_id = uuid.uuid4()

    async def _seed_and_token():
        from app.models.user import User

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            s.add(
                User(
                    id=user_id,
                    email=f"{role}@mc.local",
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
    token = await _role_client(client, "operator")()
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
async def viewer_client(client: AsyncClient) -> AsyncClient:
    token = await _role_client(client, "viewer")()
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ── helpers ────────────────────────────────────────────────────────────────


def _mk_root(tmp_path, key: str = "deliverables"):
    base = tmp_path / ".mc" / key
    base.mkdir(parents=True, exist_ok=True)
    return base


async def _seed_index(rel_path, root_key="deliverables", deliverable_id=None, is_dir=False):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = FileIndexEntry(
            root_key=root_key,
            rel_path=rel_path,
            name=rel_path.rsplit("/", 1)[-1] or root_key,
            is_directory=is_dir,
            deliverable_id=deliverable_id,
        )
        s.add(row)
        await s.commit()


async def _seed_deliverable(**kwargs) -> uuid.UUID:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        d = TaskDeliverable(
            id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            deliverable_type="file",
            title="d",
            **kwargs,
        )
        s.add(d)
        await s.commit()
        return d.id


async def _count(model, **where) -> int:
    from sqlalchemy import func
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        stmt = select(func.count()).select_from(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return (await s.exec(stmt)).one()


# ── tests ───────────────────────────────────────────────────────────────────


async def test_delete_success_trashed_under_trash(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "f.txt").write_text("data")

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["f.txt"]}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["trashed"]) == 1
    tp = data["trashed"][0]["trash_path"]
    assert "/.mc/.trash/" in tp
    assert not (base / "f.txt").exists()
    from pathlib import Path

    assert Path(tp).exists()


async def test_delete_empty_subpath_400(operator_client, tmp_path, monkeypatch):
    """CRITICAL: subpaths=[''] would trash the whole root → must 400, untouched."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "keep.txt").write_text("keep")

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": [""]}
    )
    assert resp.status_code == 400
    assert (base / "keep.txt").exists()


async def test_delete_containment_escape_400_nothing_moved(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "ok1.txt").write_text("1")
    (base / "ok2.txt").write_text("2")

    resp = await operator_client.post(
        "/api/v1/files/delete",
        json={"root": "deliverables", "subpaths": ["ok1.txt", "ok2.txt", "../../escape"]},
    )
    assert resp.status_code == 400
    # two-phase: earlier files still on disk (nothing moved)
    assert (base / "ok1.txt").exists()
    assert (base / "ok2.txt").exists()


async def test_blocked_roots_403(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    for root in ("workspaces", "vault", "attachments", "shared-deliverables"):
        base = tmp_path / ".mc" / root
        base.mkdir(parents=True, exist_ok=True)
        (base / "x.txt").write_text("x")
        resp = await operator_client.post(
            "/api/v1/files/delete", json={"root": root, "subpaths": ["x.txt"]}
        )
        assert resp.status_code == 403, root
        assert resp.json()["detail"]  # clear reason
        assert (base / "x.txt").exists()


async def test_sensitive_roots_403(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    for root in ("secrets", "agents", "logs", "backups", "browser-profiles"):
        resp = await operator_client.post(
            "/api/v1/files/delete", json={"root": root, "subpaths": ["x.txt"]}
        )
        assert resp.status_code == 403, root  # 403, NOT 404


async def test_unknown_root_404(operator_client):
    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "does-not-exist", "subpaths": ["x.txt"]}
    )
    assert resp.status_code == 404


async def test_cascade_deliverable(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "rep.txt").write_text("r")
    did = await _seed_deliverable()
    await _seed_index("rep.txt", deliverable_id=did)

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["rep.txt"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cascaded_deliverables"] == 1
    assert await _count(TaskDeliverable, id=did) == 0
    assert await _count(FileIndexEntry, rel_path="rep.txt") == 0


async def test_cascade_file_index_only(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "x.txt").write_text("x")
    await _seed_index("x.txt", deliverable_id=None)
    pre = await _count(TaskDeliverable)

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["x.txt"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cascaded_deliverables"] == 0
    assert await _count(FileIndexEntry, rel_path="x.txt") == 0
    assert await _count(TaskDeliverable) == pre


async def test_cascade_noncanonical_subpath_matches(operator_client, tmp_path, monkeypatch):
    """HIGH: delete 'a/./b.txt' must match the canonical 'a/b.txt' index row."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "a").mkdir()
    (base / "a" / "b.txt").write_text("b")
    did = await _seed_deliverable()
    await _seed_index("a/b.txt", deliverable_id=did)

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["a/./b.txt"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cascaded_deliverables"] == 1
    assert await _count(FileIndexEntry, rel_path="a/b.txt") == 0
    assert await _count(TaskDeliverable, id=did) == 0


async def test_cascade_skips_referenced_deliverable(operator_client, tmp_path, monkeypatch):
    """CRITICAL cross-project: a referenced deliverable must be KEPT."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "shared.txt").write_text("s")
    did = await _seed_deliverable()
    await _seed_index("shared.txt", deliverable_id=did)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(DeliverableReference(id=uuid.uuid4(), source_deliverable_id=did))
        await s.commit()

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["shared.txt"]}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["cascaded_deliverables"] == 0
    # file trashed + index row cleared, but deliverable kept
    assert not (base / "shared.txt").exists()
    assert await _count(FileIndexEntry, rel_path="shared.txt") == 0
    assert await _count(TaskDeliverable, id=did) == 1
    assert any(sk["reason"] == "deliverable_kept_referenced" for sk in data["skipped"])


async def test_cascade_nulls_sibling_file_index_rows(operator_client, tmp_path, monkeypatch):
    """HIGH FK: a sibling row pointing at the same deliverable must be NULLed."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "one.txt").write_text("1")
    (base / "two.txt").write_text("2")
    did = await _seed_deliverable()
    await _seed_index("one.txt", deliverable_id=did)
    await _seed_index("two.txt", deliverable_id=did)

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["one.txt"]}
    )
    assert resp.status_code == 200, resp.text  # no IntegrityError
    assert resp.json()["cascaded_deliverables"] == 1
    # sibling row survives with deliverable_id NULLed
    from sqlmodel import select

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        sib = (
            await s.exec(select(FileIndexEntry).where(FileIndexEntry.rel_path == "two.txt"))
        ).first()
        assert sib is not None
        assert sib.deliverable_id is None
    assert await _count(TaskDeliverable, id=did) == 0


async def test_directory_delete_cascades_children(operator_client, tmp_path, monkeypatch):
    """HIGH over-match: trash 'reports' dir → its rows + children gone, 'reports2' safe."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "reports").mkdir()
    (base / "reports" / "a.txt").write_text("a")
    (base / "reports2").mkdir()
    (base / "reports2" / "b.txt").write_text("b")
    await _seed_index("reports", is_dir=True)
    await _seed_index("reports/a.txt")
    await _seed_index("reports2", is_dir=True)
    await _seed_index("reports2/b.txt")

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["reports"]}
    )
    assert resp.status_code == 200, resp.text
    assert await _count(FileIndexEntry, rel_path="reports") == 0
    assert await _count(FileIndexEntry, rel_path="reports/a.txt") == 0
    # sibling dir 'reports2' untouched (boundary-safe LIKE)
    assert await _count(FileIndexEntry, rel_path="reports2") == 1
    assert await _count(FileIndexEntry, rel_path="reports2/b.txt") == 1
    assert (base / "reports2" / "b.txt").exists()


async def test_vault_mirror_intact(operator_client, tmp_path, monkeypatch):
    """Deleting a file is not deleting a knowledge note — BoardMemory untouched."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "f.txt").write_text("f")
    await _seed_index("f.txt")

    from app.models.memory import BoardMemory

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            BoardMemory(
                id=uuid.uuid4(),
                title="note",
                content="c",
                memory_type="knowledge",
                source="user",
            )
        )
        await s.commit()
    pre = await _count(BoardMemory)

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["f.txt"]}
    )
    assert resp.status_code == 200, resp.text
    assert await _count(BoardMemory) == pre


async def test_auth_viewer_403_operator_admin_ok_unauth_401(client, tmp_path, monkeypatch):
    """One client, explicit per-request Authorization (the role fixtures all
    mutate client.headers, so they'd clobber each other in a single test)."""
    from app.auth import create_access_token
    from app.models.user import User

    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    for n in ("a.txt", "b.txt", "c.txt"):
        (base / n).write_text("x")

    tokens: dict[str, str] = {}
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for role in ("viewer", "operator", "admin"):
            uid = uuid.uuid4()
            s.add(User(id=uid, email=f"{role}@mc.local", name=role, role=role, is_active=True))
            tokens[role] = create_access_token(str(uid), role)
        await s.commit()

    # unauth → 401
    r = await client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["a.txt"]}
    )
    assert r.status_code == 401

    # viewer → 403
    r = await client.post(
        "/api/v1/files/delete",
        json={"root": "deliverables", "subpaths": ["a.txt"]},
        headers={"Authorization": f"Bearer {tokens['viewer']}"},
    )
    assert r.status_code == 403
    assert (base / "a.txt").exists()

    # operator → 200
    r = await client.post(
        "/api/v1/files/delete",
        json={"root": "deliverables", "subpaths": ["b.txt"]},
        headers={"Authorization": f"Bearer {tokens['operator']}"},
    )
    assert r.status_code == 200, r.text

    # admin → 200
    r = await client.post(
        "/api/v1/files/delete",
        json={"root": "deliverables", "subpaths": ["c.txt"]},
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert r.status_code == 200, r.text


async def test_batch_single_timestamp(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    for n in ("a.txt", "b.txt", "c.txt"):
        (base / n).write_text(n)

    resp = await operator_client.post(
        "/api/v1/files/delete",
        json={"root": "deliverables", "subpaths": ["a.txt", "b.txt", "c.txt"]},
    )
    assert resp.status_code == 200, resp.text
    paths = [t["trash_path"] for t in resp.json()["trashed"]]
    # all under the same <ts>/ segment
    stamps = {p.split("/.trash/")[1].split("/")[0] for p in paths}
    assert len(stamps) == 1


async def test_max_batch_422(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _mk_root(tmp_path)
    subpaths = [f"f{i}.txt" for i in range(201)]
    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": subpaths}
    )
    assert resp.status_code == 422


async def test_trashed_file_never_indexed(operator_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = _mk_root(tmp_path)
    (base / "gone.txt").write_text("g")

    resp = await operator_client.post(
        "/api/v1/files/delete", json={"root": "deliverables", "subpaths": ["gone.txt"]}
    )
    assert resp.status_code == 200, resp.text

    # reindex: the file now lives under ~/.mc/.trash — must never be returned
    r = await operator_client.post("/api/v1/files/reindex")
    assert r.status_code == 200
    s = await operator_client.get("/api/v1/files/search", params={"q": "gone"})
    names = {x["name"] for x in s.json()["results"]}
    assert "gone.txt" not in names
    # and it isn't listed under any deliverables subpath
    lst = await operator_client.get(
        "/api/v1/files/list", params={"root": "deliverables", "subpath": ""}
    )
    if lst.status_code == 200:
        assert "gone.txt" not in {e["name"] for e in lst.json()["entries"]}
