"""File index repair (knowledge concept P1 + P2).

Live finding (2026-11-23): one global 50 000-entry cap was eaten by the
``workspaces`` root (code checkouts, ~900k entries) every round, so vault,
media, references, screenshots and shared-artifacts held one row each and new
files there were never found; every ~10 min all 50k rows were rewritten.

These tests pin the repaired behaviour:
* ``workspaces`` is not walked; its walk leftovers are pruned, captured
  deliverable rows stay.
* the cap applies per root, so one big root cannot starve the others.
* a round writes only new / changed rows and deletes vanished ones.
* rows of roots that are no longer registered are pruned.
* a truncated root keeps rows that were not reached but still exist on disk.
* one INFO line per round carries the per-root counts.
* a newly registered deliverable gets its index row at once (capture wired
  into the real deliverable-create path).
"""

from __future__ import annotations

import logging
import os
import uuid

from httpx import AsyncClient
from sqlalchemy import event
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.deliverable import TaskDeliverable
from app.models.file_index import FileIndexEntry
from app.services import file_indexer, fs_roots
from app.services.file_indexer import run_once
from app.services.fs_roots import browsable_roots
from tests.conftest import test_engine


def _factory() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


def _host_roots_only(monkeypatch) -> None:
    """Keep the named-volume root (/shared-deliverables, real on dev boxes)
    out of the walk — every other root derives from home_host=tmp_path."""
    keep = [r for r in browsable_roots() if r.container_override is None]
    monkeypatch.setattr(file_indexer, "browsable_roots", lambda: keep)
    monkeypatch.setattr(fs_roots, "browsable_roots", lambda: keep)


async def _rows(session: AsyncSession, root: str | None = None) -> list[FileIndexEntry]:
    stmt = select(FileIndexEntry)
    if root is not None:
        stmt = stmt.where(FileIndexEntry.root_key == root)
    return list((await session.exec(stmt)).all())


def _mk(path, data: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


# --- P1a: workspaces out of the walk --------------------------------------

async def test_workspaces_not_walked_and_leftovers_pruned(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    mc = tmp_path / ".mc"
    _mk(mc / "workspaces" / "agent" / "repo" / "main.py", "print(1)")
    _mk(mc / "vault" / "note.md", "# n")

    # A leftover row from the old walk, and a row captured for a deliverable.
    deliv = TaskDeliverable(task_id=uuid.uuid4(), deliverable_type="file", title="d", path="/x/y")
    async_session.add(deliv)
    await async_session.commit()
    await async_session.refresh(deliv)
    async_session.add(FileIndexEntry(root_key="workspaces", rel_path="old/walked.py", name="walked.py"))
    async_session.add(
        FileIndexEntry(
            root_key="workspaces", rel_path="agent/out.pdf", name="out.pdf",
            deliverable_id=deliv.id, task_id=deliv.task_id,
        )
    )
    await async_session.commit()

    result = await run_once(session_factory=_factory)

    async with _factory() as s:
        ws = {r.rel_path for r in await _rows(s, "workspaces")}
        vault = {r.rel_path for r in await _rows(s, "vault")}
    assert "workspaces" not in result["roots"]
    assert ws == {"agent/out.pdf"}, ws  # walk leftover gone, captured row kept
    assert "note.md" in vault


# --- P1b: cap per root, not global ----------------------------------------

async def test_cap_is_per_root_not_global(async_session, tmp_path, monkeypatch):
    """Sabotage target: a global cap lets the first big root (deliverables)
    eat the budget, leaving vault/media with one row each — the live bug."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    mc = tmp_path / ".mc"
    for i in range(30):
        _mk(mc / "deliverables" / f"f{i:02d}.txt")
    for i in range(3):
        _mk(mc / "vault" / f"n{i}.md")
    for i in range(2):
        _mk(mc / "media" / f"m{i}.png")

    result = await run_once(session_factory=_factory, max_entries=10)

    async with _factory() as s:
        counts = {k: len(await _rows(s, k)) for k in ("deliverables", "vault", "media")}
    assert counts == {"deliverables": 10, "vault": 3, "media": 2}, counts
    assert result["truncated_roots"] == ["deliverables"]
    assert result["per_root"]["vault"] == 3
    assert result["per_root"]["media"] == 2


# --- P1c: write only what changed -----------------------------------------

def _count_writes(sync_engine):
    """Rows actually written to file_index (a no-op DELETE counts 0)."""
    seen = {"insert": 0, "update": 0, "delete": 0}

    def after(conn, cursor, statement, params, context, executemany):
        st = statement.lstrip().upper()
        if "FILE_INDEX" not in st.split("WHERE")[0]:
            return
        rc = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        if st.startswith("INSERT"):
            seen["insert"] += rc or (len(params) if executemany else 1)
        elif st.startswith("UPDATE"):
            seen["update"] += rc
        elif st.startswith("DELETE"):
            seen["delete"] += rc

    return seen, after


async def test_unchanged_round_writes_nothing(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    vault = tmp_path / ".mc" / "vault"
    for i in range(20):
        _mk(vault / f"n{i}.md", "a")

    r1 = await run_once(session_factory=_factory)
    assert r1["inserted"] == 20

    seen, after = _count_writes(test_engine.sync_engine)
    event.listen(test_engine.sync_engine, "after_cursor_execute", after)
    try:
        r2 = await run_once(session_factory=_factory)
    finally:
        event.remove(test_engine.sync_engine, "after_cursor_execute", after)
    assert seen == {"insert": 0, "update": 0, "delete": 0}, seen
    assert (r2["inserted"], r2["updated"], r2["pruned"]) == (0, 0, 0)

    # one file changes, one vanishes, one is new → exactly those rows move
    changed = vault / "n3.md"
    changed.write_text("longer content")
    os.utime(changed, (1_900_000_000, 1_900_000_000))
    (vault / "n4.md").unlink()
    _mk(vault / "fresh.md", "new")
    r3 = await run_once(session_factory=_factory)
    assert (r3["inserted"], r3["updated"], r3["pruned"]) == (1, 1, 1), r3

    async with _factory() as s:
        rows = {r.rel_path: r for r in await _rows(s, "vault")}
    assert "n4.md" not in rows and "fresh.md" in rows
    assert rows["n3.md"].size == len("longer content")
    assert rows["n3.md"].mtime == 1_900_000_000


# --- P1d: orphan roots + truncated roots ----------------------------------

async def test_rows_of_unregistered_root_pruned(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    _mk(tmp_path / ".mc" / "vault" / "n.md")
    async_session.add(FileIndexEntry(root_key="storyboard-images", rel_path="", name="storyboard-images"))
    async_session.add(FileIndexEntry(root_key="secrets", rel_path="token.txt", name="token.txt"))
    await async_session.commit()

    await run_once(session_factory=_factory)

    async with _factory() as s:
        keys = {r.root_key for r in await _rows(s)}
    assert "storyboard-images" not in keys
    assert "secrets" not in keys
    assert "vault" in keys


async def test_truncated_root_keeps_unreached_rows_that_still_exist(async_session, tmp_path, monkeypatch):
    """With a cap, the walk stops early. A row the walk did not reach must not
    be deleted while its file still exists — that row may be a captured
    deliverable (with task/agent provenance) that only the capture wrote."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    d = tmp_path / ".mc" / "deliverables"
    for i in range(6):
        _mk(d / f"top{i}.txt")
    _mk(d / "z" / "deep.pdf", "%PDF")  # top-down walk reaches this last
    async_session.add(FileIndexEntry(root_key="deliverables", rel_path="z/deep.pdf", name="deep.pdf", task_id=uuid.uuid4()))
    async_session.add(FileIndexEntry(root_key="deliverables", rel_path="gone.txt", name="gone.txt"))
    await async_session.commit()

    r = await run_once(session_factory=_factory, max_entries=3)
    assert r["truncated_roots"] == ["deliverables"]

    async with _factory() as s:
        rels = {x.rel_path for x in await _rows(s, "deliverables")}
    assert "z/deep.pdf" in rels  # exists on disk, just not reached
    assert "gone.txt" not in rels  # vanished from disk


# --- P1e: one per-root log line per round ---------------------------------

async def test_one_info_line_with_per_root_counts(async_session, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    _mk(tmp_path / ".mc" / "vault" / "a.md")
    _mk(tmp_path / ".mc" / "media" / "b.png")

    with caplog.at_level(logging.INFO, logger="mc.file_indexer"):
        r = await run_once(session_factory=_factory)

    lines = [rec.getMessage() for rec in caplog.records if "file_indexer round" in rec.getMessage()]
    assert len(lines) == 1, lines
    assert "vault=1" in lines[0] and "media=1" in lines[0], lines[0]
    assert r["duration_s"] >= 0


# --- P2: capture wired into the real deliverable-create path --------------

async def test_new_deliverable_gets_index_row_at_once(client: AsyncClient, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch

    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    # the capture opens its own session (it runs after the response) — bind it
    # to the test engine
    monkeypatch.setattr(file_indexer, "async_session_maker", _factory)

    async with _factory() as s:
        board = Board(id=uuid.uuid4(), name="B", slug="b")
        raw, token_hash = generate_agent_token()
        agent = Agent(
            id=uuid.uuid4(), name="Writer", board_id=board.id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "knowledge:read", "knowledge:write"],
        )
        task = Task(id=uuid.uuid4(), board_id=board.id, title="T", status="in_progress", assigned_agent_id=agent.id)
        s.add_all([board, agent, task])
        await s.commit()
        board_id, task_id = board.id, task.id

    f = tmp_path / ".mc" / "deliverables" / str(task_id) / "report.pdf"
    _mk(f, "%PDF-1.4 hello")

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
            json={"deliverable_type": "file", "title": "Report", "path": str(f)},
            headers={"Authorization": f"Bearer {raw}"},
        )
    assert resp.status_code == 201, resp.text

    async with _factory() as s:
        rows = (
            await s.exec(
                select(FileIndexEntry).where(
                    FileIndexEntry.root_key == "deliverables",
                    FileIndexEntry.rel_path == f"{task_id}/report.pdf",
                )
            )
        ).all()
    assert len(rows) == 1, "deliverable was not indexed at registration"
    assert str(rows[0].deliverable_id) == resp.json()["id"]
    assert rows[0].task_id == task_id
    assert rows[0].size == len("%PDF-1.4 hello")


# --- Races between the walk and a capture (critic finding, minor) ---------

async def test_walk_keeps_captured_row_created_after_the_scan(async_session, tmp_path, monkeypatch):
    """A deliverable written + captured after the walk passed its folder is in
    the DB rows but not in the scan. Its file exists, so the row (with the
    provenance only the capture knows) must stay."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    vault = tmp_path / ".mc" / "vault"
    _mk(vault / "seen.md")

    real_scan = file_indexer._scan_root

    def scan_before_capture(base, max_entries):
        entries, truncated = real_scan(base, max_entries)
        if base == vault:
            # the capture lands between the walk (phase 1) and the row read
            _mk(vault / "late.pdf", "%PDF")
        return entries, truncated

    monkeypatch.setattr(file_indexer, "_scan_root", scan_before_capture)
    # The captured row (as the capture writes it) is already in the DB when
    # run_once reads rows; the scan above does not contain the file.
    task_id = uuid.uuid4()
    async_session.add(
        FileIndexEntry(root_key="vault", rel_path="late.pdf", name="late.pdf", task_id=task_id, agent_slug="writer")
    )
    await async_session.commit()

    await run_once(session_factory=_factory)

    async with _factory() as s:
        rows = {r.rel_path: r for r in await _rows(s, "vault")}
    assert "late.pdf" in rows, "captured row deleted although its file exists"
    assert rows["late.pdf"].task_id == task_id and rows["late.pdf"].agent_slug == "writer"


async def test_walk_still_deletes_captured_row_whose_file_is_gone(async_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    _host_roots_only(monkeypatch)
    _mk(tmp_path / ".mc" / "vault" / "keep.md")
    async_session.add(FileIndexEntry(root_key="vault", rel_path="gone.pdf", name="gone.pdf", task_id=uuid.uuid4()))
    await async_session.commit()

    await run_once(session_factory=_factory)

    async with _factory() as s:
        rels = {r.rel_path for r in await _rows(s, "vault")}
    assert "gone.pdf" not in rels


async def test_capture_retries_once_when_the_walk_inserted_the_row_first(async_session, monkeypatch):
    """The walk can insert the plain row between the capture's read and its
    commit → unique (root_key, rel_path) IntegrityError. One retry turns it
    into an update, so the provenance is not lost."""
    from sqlalchemy.exc import IntegrityError

    deliv = TaskDeliverable(task_id=uuid.uuid4(), deliverable_type="file", title="d", path="/x/y")
    async_session.add(deliv)
    await async_session.commit()
    await async_session.refresh(deliv)

    calls = {"n": 0}

    async def flaky_capture(session, deliverable, agent=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT INTO file_index", {}, Exception("unique"))
        return await file_indexer._upsert(
            session, "vault", "y", name="y", is_directory=False, size=1, mime=None, mtime=1.0,
            task_id=deliverable.task_id, deliverable_id=deliverable.id,
        )

    monkeypatch.setattr(file_indexer, "capture_deliverable", flaky_capture)
    ok = await file_indexer.capture_deliverable_id(deliv.id, session_factory=_factory)

    assert ok is True and calls["n"] == 2
    async with _factory() as s:
        row = (await s.exec(select(FileIndexEntry).where(FileIndexEntry.rel_path == "y"))).one()
    assert row.deliverable_id == deliv.id
