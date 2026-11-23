"""Postgres lane: the files-index walk against the real schema.

The SQLite lane cannot show two things this walk relies on:
* ``mtime`` round-trips exactly through ``double precision`` — otherwise
  every round "updates" every row again (the churn the per-root fix removed);
* the orphan prune (``root_key NOT IN … AND deliverable_id IS NULL AND
  task_id IS NULL``) runs as one statement and reports its row count.

Run: MC_TEST_DATABASE_URL=postgresql+asyncpg://… after ``alembic upgrade head``.
"""

from __future__ import annotations

import os

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.file_index import FileIndexEntry
from app.services import file_indexer, fs_roots
from app.services.file_indexer import run_once
from tests.conftest import test_engine

pytestmark = pytest.mark.postgres


def _factory() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


async def test_second_round_writes_nothing_and_workspaces_leftovers_go(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    keep = [r for r in fs_roots.browsable_roots() if r.container_override is None]
    monkeypatch.setattr(file_indexer, "browsable_roots", lambda: keep)
    monkeypatch.setattr(fs_roots, "browsable_roots", lambda: keep)

    vault = tmp_path / ".mc" / "vault"
    vault.mkdir(parents=True)
    for i in range(25):
        f = vault / f"n{i}.md"
        f.write_text("x" * i)
        # sub-second mtimes: the part a float/real column would round away
        os.utime(f, (1_800_000_000.123456 + i, 1_800_000_000.123456 + i))

    async with _factory() as s:
        s.add(FileIndexEntry(root_key="workspaces", rel_path="repo/a.py", name="a.py"))
        s.add(FileIndexEntry(root_key="workspaces", rel_path="repo/b.py", name="b.py"))
        await s.commit()

    r1 = await run_once(session_factory=_factory)
    assert r1["inserted"] == 25
    assert r1["pruned"] == 2, r1  # the two workspaces leftovers, rowcount reported

    r2 = await run_once(session_factory=_factory)
    assert (r2["inserted"], r2["updated"], r2["pruned"]) == (0, 0, 0), r2

    async with _factory() as s:
        ws = (await s.exec(select(FileIndexEntry).where(FileIndexEntry.root_key == "workspaces"))).all()
    assert ws == []
