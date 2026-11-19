"""Knowledge in one place, slice 1 (ADR-104): layout, project folders, index,
read API, flag.

The files are the truth, the table ``knowledge_entries`` is only an index.
Every test runs in the stand-in host home of conftest (``settings.home_host``)
and a temporary vault, never in a real ~/.mc.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.board import Project
from app.models.knowledge_entry import KnowledgeEntry
from app.models.repo import Repo
from app.services import knowledge, knowledge_import
from tests.conftest import test_engine


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_root_enabled", True, raising=False)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A fresh MC home (knowledge root + skills) and a fresh vault."""
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    (home / ".mc").mkdir(parents=True)
    vault.mkdir()
    monkeypatch.setattr(settings, "home_host", str(home))
    monkeypatch.setattr(settings, "vault_path", vault)
    return home / ".mc", vault


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _project(make_board, name: str, **kw) -> Project:
    board = await make_board(name=f"B {name}", slug=f"b-{uuid.uuid4().hex[:6]}")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        p = Project(board_id=board.id, name=name, **kw)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p


async def _sync():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await knowledge_import.sync_all(s)


async def _rows() -> list[KnowledgeEntry]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return list((await s.exec(select(KnowledgeEntry))).all())


# ── layout + project folders ────────────────────────────────────────────────


async def test_sync_does_nothing_while_flag_is_off(homes):
    mc, _vault = homes
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await knowledge_import.knowledge_sync.sweep_once() is None
    assert not (mc / "knowledge").exists()
    assert await _rows() == []


async def test_layout_and_one_folder_per_project(homes, flag_on, make_board):
    mc, _vault = homes
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        repo = Repo(full_name="acme/shop", url="https://github.com/acme/shop", rules_md="Always run the tests.")
        s.add(repo)
        await s.commit()
        await s.refresh(repo)
    p = await _project(
        make_board, "Online Shop", description="Sell shoes.", briefing_doc="Phase 1 done.",
        repo_id=repo.id, status="active",
    )
    stats = await _sync()
    root = mc / "knowledge"
    for drawer in ("notes", "skills", "projects"):
        assert (root / drawer).is_dir()
    assert (root / "README.md").is_file()

    folder = root / "projects" / "online-shop"
    brief = (folder / "PROJECT.md").read_text()
    assert "# Online Shop" in brief and "Sell shoes." in brief and "Phase 1 done." in brief
    assert "acme/shop" in brief and "Always run the tests." in brief
    assert (folder / "knowledge").is_dir()
    meta = json.loads((folder / ".mc" / "project.json").read_text())
    assert meta["id"] == str(p.id) and meta["slug"] == "online-shop"
    assert stats.projects_created == 1

    rows = await _rows()
    brief_rows = [r for r in rows if r.kind == "project"]
    assert len(brief_rows) == 1
    assert brief_rows[0].drawer == "projects" and brief_rows[0].project_slug == "online-shop"
    assert brief_rows[0].project_id == p.id


async def test_slug_is_stable_on_rename_and_unique_on_clash(homes, flag_on, make_board):
    mc, _vault = homes
    a = await _project(make_board, "Shop")
    b = await _project(make_board, "shop!")
    await _sync()
    folders = sorted(x.name for x in (mc / "knowledge" / "projects").iterdir())
    assert len(folders) == 2 and "shop" in folders
    other = next(f for f in folders if f != "shop")
    assert other.startswith("shop-")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Project, a.id)
        row.name = "Totally New Name"
        s.add(row)
        await s.commit()
    await _sync()
    assert sorted(x.name for x in (mc / "knowledge" / "projects").iterdir()) == folders
    owner = json.loads((mc / "knowledge" / "projects" / "shop" / ".mc" / "project.json").read_text())["id"]
    renamed = "shop" if owner == str(a.id) else other
    assert "# Totally New Name" in (mc / "knowledge" / "projects" / renamed / "PROJECT.md").read_text()
    assert b.id  # second project still has its own folder


async def test_an_edited_brief_is_never_overwritten(homes, flag_on, make_board):
    mc, _vault = homes
    p = await _project(make_board, "Garden", description="Old text")
    await _sync()
    brief = mc / "knowledge" / "projects" / "garden" / "PROJECT.md"
    brief.write_text(brief.read_text() + "\nMy own line.\n")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Project, p.id)
        row.description = "New text from the DB"
        s.add(row)
        await s.commit()
    stats = await _sync()
    text = brief.read_text()
    assert "My own line." in text and "New text from the DB" not in text
    assert stats.briefs_kept == 1


async def test_an_unedited_brief_follows_the_db(homes, flag_on, make_board):
    mc, _vault = homes
    p = await _project(make_board, "Garden", description="Old text")
    await _sync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Project, p.id)
        row.description = "New text from the DB"
        s.add(row)
        await s.commit()
    await _sync()
    assert "New text from the DB" in (mc / "knowledge" / "projects" / "garden" / "PROJECT.md").read_text()


# ── index ───────────────────────────────────────────────────────────────────


async def test_existing_sources_are_indexed_and_never_written(homes, flag_on):
    mc, vault = homes
    v1 = _write(vault / "global" / "decisions" / "d1.md", "---\nid: d1\ntype: knowledge\ntitle: Use files\ntags: [arch]\n---\nFiles first.\n")
    v2 = _write(vault / "agents" / "rex" / "lessons" / "l1.md", "---\nid: l1\ntype: lesson\n---\n# Lesson one\nBody.\n")
    _write(vault / "_trash" / "gone.md", "trash")
    _write(vault / "attachments" / "files" / "a.md", "attachment")
    s1 = _write(mc / "skills" / "deploy" / "SKILL.md", "---\nname: deploy\ndescription: Ship it\n---\nSteps.\n")
    _write(mc / "skills" / "_archived-2026" / "old" / "SKILL.md", "old")
    before = {p: (_sha(p), p.stat().st_mtime) for p in (v1, v2, s1)}

    stats = await _sync()
    rows = {(r.source, r.rel_path): r for r in await _rows()}
    assert ("vault", "global/decisions/d1.md") in rows
    assert ("vault", "agents/rex/lessons/l1.md") in rows
    assert ("skills", "deploy/SKILL.md") in rows
    assert not any("_trash" in k[1] or "attachments" in k[1] or "_archived" in k[1] for k in rows)
    d1 = rows[("vault", "global/decisions/d1.md")]
    assert d1.drawer == "notes" and d1.title == "Use files" and d1.tags == ["arch"]
    assert rows[("vault", "agents/rex/lessons/l1.md")].title == "Lesson one"
    sk = rows[("skills", "deploy/SKILL.md")]
    assert sk.drawer == "skills" and sk.kind == "skill" and sk.title == "deploy"
    assert stats.indexed_new == 3
    for p, (sha, mtime) in before.items():
        assert (_sha(p), p.stat().st_mtime) == (sha, mtime), f"{p} was changed"
    assert not (vault / "projects").exists()


async def test_index_follows_the_files(homes, flag_on):
    mc, _vault = homes
    note = _write(mc / "knowledge" / "notes" / "inbox" / "n.md", "# First\nalpha\n")
    await _sync()
    second = await _sync()
    assert second.unchanged >= 1 and second.updated == 0 and second.indexed_new == 0

    note.write_text("# Second\nbeta\n")
    os.utime(note, (1_800_000_000, 1_800_000_000))
    third = await _sync()
    assert third.updated == 1
    row = next(r for r in await _rows() if r.rel_path == "notes/inbox/n.md")
    assert row.title == "Second" and "beta" in row.search_text

    note.unlink()
    fourth = await _sync()
    assert fourth.removed == 1
    assert not any(r.rel_path == "notes/inbox/n.md" for r in await _rows())


async def test_project_note_with_wrong_project_is_flagged(homes, flag_on, make_board):
    mc, _vault = homes
    await _project(make_board, "Alpha")
    await _project(make_board, "Beta")
    await _sync()
    good = mc / "knowledge" / "projects" / "alpha" / "knowledge" / "ok.md"
    bad = mc / "knowledge" / "projects" / "alpha" / "knowledge" / "wrong.md"
    _write(good, "---\nproject: alpha\n---\n# Fine\n")
    _write(bad, "---\nproject: beta\n---\n# Wrong home\n")
    stats = await _sync()
    rows = {r.rel_path: r for r in await _rows()}
    assert rows["projects/alpha/knowledge/ok.md"].problem is None
    assert rows["projects/alpha/knowledge/ok.md"].project_slug == "alpha"
    assert rows["projects/alpha/knowledge/wrong.md"].problem == "project_mismatch"
    assert stats.problems == 1
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        listed = await knowledge.list_entries(s, project="alpha")
    titles = [e["title"] for e in listed["entries"]]
    assert "Fine" in titles and "Wrong home" not in titles
    assert bad.exists()  # flagged, never moved or deleted


async def test_vault_note_lands_in_its_project_by_frontmatter(homes, flag_on, make_board):
    _mc, vault = homes
    await _project(make_board, "Alpha")
    _write(vault / "agents" / "rex" / "k.md", "---\nproject: alpha\n---\n# For alpha\n")
    _write(vault / "agents" / "rex" / "k2.md", "---\nproject: not-a-project\n---\n# Loose\n")
    await _sync()
    rows = {r.rel_path: r for r in await _rows()}
    assert rows["agents/rex/k.md"].project_slug == "alpha"
    assert rows["agents/rex/k2.md"].project_slug is None
    assert rows["agents/rex/k2.md"].drawer == "notes"


async def test_symlink_out_of_the_root_is_not_indexed(homes, flag_on, tmp_path):
    mc, _vault = homes
    secret = _write(tmp_path / "outside" / "secret.md", "# Secret\n")
    target = mc / "knowledge" / "notes"
    target.mkdir(parents=True, exist_ok=True)
    os.symlink(secret, target / "link.md")
    await _sync()
    assert not any(r.rel_path == "notes/link.md" for r in await _rows())


async def test_search_text_is_capped(homes, flag_on):
    mc, _vault = homes
    _write(mc / "knowledge" / "notes" / "big.md", "# Big\n" + "x" * (knowledge.SEARCH_TEXT_CAP * 2))
    await _sync()
    row = next(r for r in await _rows() if r.rel_path == "notes/big.md")
    assert len(row.search_text) <= knowledge.SEARCH_TEXT_CAP


# ── read API ────────────────────────────────────────────────────────────────


async def test_api_is_404_while_flag_is_off(auth_client):
    for url in (
        "/api/v1/knowledge-root/status",
        "/api/v1/knowledge-root/projects",
        "/api/v1/knowledge-root/entries",
        "/api/v1/knowledge-root/search?q=abc",
    ):
        r = await auth_client.get(url)
        assert r.status_code == 404, url
        assert r.json()["detail"]["reason"] == "knowledge_root_disabled"
    r = await auth_client.post("/api/v1/knowledge-root/_admin/sync")
    assert r.status_code == 404


async def test_api_lists_reads_and_searches(homes, flag_on, make_board, auth_client):
    mc, vault = homes
    await _project(make_board, "Alpha", description="The alpha project")
    _write(vault / "global" / "d.md", "---\ntitle: Files first\n---\nWe keep markdown as the truth.\n")
    r = await auth_client.post("/api/v1/knowledge-root/_admin/sync")
    assert r.status_code == 200 and r.json()["projectsCreated"] == 1
    _write(mc / "knowledge" / "projects" / "alpha" / "knowledge" / "n.md", "# Alpha note\nzebra crossing\n")
    await auth_client.post("/api/v1/knowledge-root/_admin/sync")

    st = (await auth_client.get("/api/v1/knowledge-root/status")).json()
    assert st["enabled"] is True and st["counts"]["projects"] == 2 and st["counts"]["notes"] == 1
    assert st["lastSync"] is not None

    projects = (await auth_client.get("/api/v1/knowledge-root/projects")).json()["projects"]
    assert [p["slug"] for p in projects] == ["alpha"]
    assert projects[0]["name"] == "Alpha" and projects[0]["entries"] == 2

    listed = (await auth_client.get("/api/v1/knowledge-root/entries?project=alpha")).json()
    assert {e["title"] for e in listed["entries"]} == {"Alpha", "Alpha note"}
    notes = (await auth_client.get("/api/v1/knowledge-root/entries?drawer=notes")).json()["entries"]
    assert [e["source"] for e in notes] == ["vault"]

    note = next(e for e in listed["entries"] if e["title"] == "Alpha note")
    one = (await auth_client.get(f"/api/v1/knowledge-root/entries/{note['id']}")).json()
    assert "zebra crossing" in one["body"] and one["stale"] is False

    hits = (await auth_client.get("/api/v1/knowledge-root/search?q=zebra")).json()["hits"]
    assert [h["title"] for h in hits] == ["Alpha note"]
    assert "zebra" in hits[0]["snippet"]
    hits = (await auth_client.get("/api/v1/knowledge-root/search?q=markdown&drawer=notes")).json()["hits"]
    assert [h["title"] for h in hits] == ["Files first"]


async def test_api_entry_whose_file_is_gone_is_410(homes, flag_on, auth_client):
    mc, _vault = homes
    note = _write(mc / "knowledge" / "notes" / "n.md", "# Gone soon\n")
    await auth_client.post("/api/v1/knowledge-root/_admin/sync")
    entry = (await auth_client.get("/api/v1/knowledge-root/entries")).json()["entries"][0]
    note.unlink()
    r = await auth_client.get(f"/api/v1/knowledge-root/entries/{entry['id']}")
    assert r.status_code == 410


async def test_api_is_admin_only(homes, flag_on, client):
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email="op@mc.local", name="Op", role="operator", is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), 'operator')}"
    assert (await client.get("/api/v1/knowledge-root/status")).status_code == 403
