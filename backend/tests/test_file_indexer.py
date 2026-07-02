"""Tests for the file index — stable agent slug, capture-at-write, walk/prune."""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import select

from app.config import settings
from app.models.agent import Agent
from app.models.deliverable import TaskDeliverable
from app.models.file_index import FileIndexEntry
from app.services.file_indexer import (
    capture_deliverable,
    reusable_deliverables,
    run_once,
)


# --- T4: stable agent slug -------------------------------------------------

async def test_agent_slug_autoset_from_name(async_session):
    a = Agent(name="Free Code")
    async_session.add(a)
    await async_session.commit()
    await async_session.refresh(a)
    assert a.slug == "free-code"


async def test_agent_slug_explicit_preserved(async_session):
    b = Agent(name="Whatever", slug="custom")
    async_session.add(b)
    await async_session.commit()
    await async_session.refresh(b)
    assert b.slug == "custom"


async def test_agent_slug_stable_across_rename(async_session):
    a = Agent(name="Free Code")
    async_session.add(a)
    await async_session.commit()
    await async_session.refresh(a)
    assert a.slug == "free-code"
    # rename → slug must NOT change (the whole point of persisting it)
    a.name = "Renamed Agent"
    async_session.add(a)
    await async_session.commit()
    await async_session.refresh(a)
    assert a.slug == "free-code"


# --- T5: capture-at-write --------------------------------------------------

async def test_capture_deliverable_creates_index_entry(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    task_id = uuid.uuid4()
    deliv_dir = tmp_path / ".mc" / "deliverables" / str(task_id)
    deliv_dir.mkdir(parents=True)
    payload = b"%PDF-1.4 hello"
    (deliv_dir / "report.pdf").write_bytes(payload)

    deliv = TaskDeliverable(
        task_id=task_id, agent_id=None, deliverable_type="file",
        title="R", path=f"{tmp_path}/.mc/deliverables/{task_id}/report.pdf",
    )
    async_session.add(deliv)
    await async_session.commit()
    await async_session.refresh(deliv)

    entry = await capture_deliverable(async_session, deliv)
    assert entry is not None
    assert entry.root_key == "deliverables"
    assert entry.rel_path == f"{task_id}/report.pdf"
    assert entry.is_directory is False
    assert entry.size == len(payload)
    assert entry.mime == "application/pdf"
    assert entry.deliverable_id == deliv.id


async def test_capture_skips_url_and_inline(async_session):
    url = TaskDeliverable(task_id=uuid.uuid4(), deliverable_type="url", title="U", path="https://x.com/a")
    inline = TaskDeliverable(task_id=uuid.uuid4(), deliverable_type="document", title="D", content="# inline", path=None)
    assert await capture_deliverable(async_session, url) is None
    assert await capture_deliverable(async_session, inline) is None


# --- T6: walk + prune + reusable ------------------------------------------

async def test_run_once_indexes_and_prunes(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    vault = tmp_path / ".mc" / "vault"
    sub = vault / "sub"
    sub.mkdir(parents=True)
    (vault / "note1.md").write_text("# a")
    (vault / "note2.md").write_text("# b")
    (sub / "deep.txt").write_text("x")

    r1 = await run_once(async_session)
    assert r1["indexed"] >= 4
    rows = (await async_session.exec(select(FileIndexEntry).where(FileIndexEntry.root_key == "vault"))).all()
    rels = {row.rel_path for row in rows}
    assert {"note1.md", "note2.md", "sub", "sub/deep.txt"}.issubset(rels)

    # delete a file → next walk prunes its index row
    (vault / "note1.md").unlink()
    r2 = await run_once(async_session)
    rels2 = {
        row.rel_path
        for row in (await async_session.exec(select(FileIndexEntry).where(FileIndexEntry.root_key == "vault"))).all()
    }
    assert "note1.md" not in rels2
    assert r2["pruned"] >= 1


async def test_run_once_skips_noise_dirs(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    ws = tmp_path / ".mc" / "workspaces" / "dev"
    (ws / "node_modules" / "left-pad").mkdir(parents=True)
    (ws / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1")
    (ws / "app.py").write_text("print(1)")

    await run_once(async_session)
    rels = {
        row.rel_path
        for row in (await async_session.exec(select(FileIndexEntry).where(FileIndexEntry.root_key == "workspaces"))).all()
    }
    assert "dev/app.py" in rels
    assert not any("node_modules" in r for r in rels)


async def test_reusable_deliverables(async_session):
    t = uuid.uuid4()
    d1 = TaskDeliverable(task_id=t, deliverable_type="file", title="reuse", path="/deliverables/x/a.txt", is_reusable=True)
    d2 = TaskDeliverable(task_id=t, deliverable_type="file", title="no", path="/deliverables/x/b.txt", is_reusable=False)
    async_session.add(d1)
    async_session.add(d2)
    await async_session.commit()
    rows = await reusable_deliverables(async_session)
    titles = {r.title for r in rows}
    assert "reuse" in titles
    assert "no" not in titles
