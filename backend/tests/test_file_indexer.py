"""Tests for the file index — stable agent slug, capture-at-write, walk/prune."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.deliverable import TaskDeliverable
from app.models.file_index import FileIndexEntry
from app.services import file_indexer
from app.services.file_indexer import (
    capture_deliverable,
    reusable_deliverables,
    run_once,
)
from app.services.fs_roots import browsable_roots
from tests.conftest import test_engine


def _factory() -> AsyncSession:
    """Session factory for run_once — same engine as the test fixtures."""
    return AsyncSession(test_engine, expire_on_commit=False)


def _tmp_roots_only(monkeypatch, tmp_path) -> list:
    """Pin run_once to host-backed roots (all derived from home_host=tmp_path)
    so container-only roots like shared-deliverables (real dirs on the dev
    machine) can't leak into counts."""
    keep = [r for r in browsable_roots() if r.container_override is None]
    monkeypatch.setattr(file_indexer, "browsable_roots", lambda: keep)
    return keep


# The SKIP_DIRS list as it was before the exclusion-list extension (parity
# reference: "the index must contain the same entries as before, minus the
# deliberately excluded").
_OLD_SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "__pycache__", ".next", ".turbo", "dist", "build", ".trash"}
)


def _reference_entries(roots) -> set[tuple[str, str]]:
    """Old run_once walk (pre-change): every path under the given roots,
    pruned only by the OLD skip list. Independent of the new implementation."""
    out: set[tuple[str, str]] = set()
    for r in roots:
        base = r.container_path
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _OLD_SKIP_DIRS]
            for nm in [*dirnames, *filenames]:
                rel = str((Path(dirpath) / nm).relative_to(base))
                out.add((r.key, rel))
    return out


def _newly_excluded(rel: str) -> bool:
    """True when a path sits under a dir that only the NEW skip list prunes."""
    for part in rel.split("/"):
        p = part.lower()
        if p in file_indexer.SKIP_DIRS and p not in _OLD_SKIP_DIRS:
            return True
        if p.endswith(file_indexer.SKIP_DIR_SUFFIXES):
            return True
    return False


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

    r1 = await run_once(session_factory=_factory)
    assert r1["indexed"] >= 4
    rows = (await async_session.exec(select(FileIndexEntry).where(FileIndexEntry.root_key == "vault"))).all()
    rels = {row.rel_path for row in rows}
    assert {"note1.md", "note2.md", "sub", "sub/deep.txt"}.issubset(rels)

    # delete a file → next walk prunes its index row
    (vault / "note1.md").unlink()
    r2 = await run_once(session_factory=_factory)
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
    # extended exclusion list: agent-workspace noise that used to be counted
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "python").write_text("")
    (ws / "venv" / "lib").mkdir(parents=True)
    (ws / "venv" / "lib" / "x.py").write_text("")
    (ws / ".pytest_cache" / "v").mkdir(parents=True)
    (ws / ".pytest_cache" / "v" / "cache").write_text("")
    (ws / "target" / "debug").mkdir(parents=True)
    (ws / "target" / "debug" / "bin").write_text("")
    (ws / "src" / "mc.egg-info").mkdir(parents=True)
    (ws / "src" / "mc.egg-info" / "PKG-INFO").write_text("")
    # case-insensitive folding (APFS deployment FS)
    (ws / "Node_Modules" / "pkg").mkdir(parents=True)
    (ws / "Node_Modules" / "pkg" / "i.js").write_text("")

    await run_once(session_factory=_factory)
    rels = {
        row.rel_path
        for row in (await async_session.exec(select(FileIndexEntry).where(FileIndexEntry.root_key == "workspaces"))).all()
    }
    assert "dev/app.py" in rels
    assert "dev/src/mc.egg-info/PKG-INFO" not in rels
    for noise in ("node_modules", ".venv", "venv", ".pytest_cache", "target", "Node_Modules"):
        assert not any(noise in r for r in rels), noise


async def test_run_once_no_transaction_during_scan(async_session, tmp_path, monkeypatch):
    """Sabotage tripwire: the filesystem walk must happen with NO open DB
    transaction. If someone re-couples the walk to a session (old shape:
    upsert-while-walking inside one transaction), the probe records open
    transactions during the walk and this test goes red."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _tmp_roots_only(monkeypatch, tmp_path)
    ws = tmp_path / ".mc" / "workspaces" / "proj"
    ws.mkdir(parents=True)
    for i in range(5):
        (ws / f"f{i}.txt").write_text("x")

    state = {"open": 0, "begins_before_first_walk": None, "open_during_walk": []}

    def on_begin(conn):
        state["open"] += 1

    def on_end(conn):
        state["open"] -= 1

    sync_engine = test_engine.sync_engine
    event.listen(sync_engine, "begin", on_begin)
    event.listen(sync_engine, "commit", on_end)
    event.listen(sync_engine, "rollback", on_end)
    real_walk = os.walk

    def probing_walk(top, *a, **k):
        if state["begins_before_first_walk"] is None:
            state["begins_before_first_walk"] = state["open"]
        state["open_during_walk"].append(state["open"])
        yield from real_walk(top, *a, **k)

    monkeypatch.setattr(file_indexer.os, "walk", probing_walk)
    try:
        result = await run_once(session_factory=_factory, batch_size=2)
    finally:
        event.remove(sync_engine, "begin", on_begin)
        event.remove(sync_engine, "commit", on_end)
        event.remove(sync_engine, "rollback", on_end)

    assert result["indexed"] == 6  # dir "proj" + dir entries + 5 files
    assert state["open_during_walk"], "probing walk never invoked"
    assert state["begins_before_first_walk"] == 0, "transaction opened before the scan"
    assert all(v == 0 for v in state["open_during_walk"]), state["open_during_walk"]


async def test_run_once_parity_with_old_walk(async_session, tmp_path, monkeypatch):
    """Gegenrichtung: the index must hold the SAME entries the old walk
    produced, minus the deliberately excluded dirs — no silent shrinkage."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    roots = _tmp_roots_only(monkeypatch, tmp_path)
    vault = tmp_path / ".mc" / "vault"
    vault.mkdir(parents=True)
    (vault / "note.md").write_text("# v")
    ws = tmp_path / ".mc" / "workspaces" / "agentx"
    (ws / "src" / "deep").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("print(1)")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "i.js").write_text("")
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "py").write_text("")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main")

    await run_once(session_factory=_factory)
    indexed = {
        (row.root_key, row.rel_path)
        for row in (await async_session.exec(select(FileIndexEntry))).all()
    }
    expected = {(root, rel) for root, rel in _reference_entries(roots) if not _newly_excluded(rel)}
    assert indexed == expected


async def test_truncation_warning_once_per_state(async_session, tmp_path, monkeypatch, caplog):
    """Hitting max_entries warns ONCE on entering the truncated state — not
    again on every identical round — and logs recovery once it ends."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _tmp_roots_only(monkeypatch, tmp_path)
    monkeypatch.setattr(file_indexer, "_truncated_last_run", False)
    vault = tmp_path / ".mc" / "vault"
    vault.mkdir(parents=True)
    for i in range(6):
        (vault / f"n{i}.md").write_text("x")

    with caplog.at_level(logging.INFO, logger="mc.file_indexer"):
        r1 = await run_once(session_factory=_factory, max_entries=4)
        r2 = await run_once(session_factory=_factory, max_entries=4)
        r3 = await run_once(session_factory=_factory, max_entries=4)

    assert r1["truncated"] is True and r1["truncated_roots"] == ["vault"]
    assert r2["truncated"] is True and r3["truncated"] is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "max_entries" in r.message]
    recoveries = [r for r in caplog.records if r.levelno == logging.INFO and "back under" in r.message]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    # recovery: cap raised above the tree size → one info line, no new warning
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="mc.file_indexer"):
        r4 = await run_once(session_factory=_factory, max_entries=100)
    assert r4["truncated"] is False
    recoveries = [r for r in caplog.records if r.levelno == logging.INFO and "back under" in r.message]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(recoveries) == 1, [r.getMessage() for r in recoveries]
    assert warnings == []


async def test_truncation_stops_the_walk(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _tmp_roots_only(monkeypatch, tmp_path)
    vault = tmp_path / ".mc" / "vault"
    vault.mkdir(parents=True)
    for i in range(10):
        (vault / f"n{i}.md").write_text("x")
    r = await run_once(session_factory=_factory, max_entries=3)
    assert r["indexed"] == 3
    assert r["truncated"] is True
    rows = (await async_session.exec(select(FileIndexEntry))).all()
    assert len(rows) == 3


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
