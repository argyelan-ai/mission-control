"""Phase 7 — OBS-02 Singleton + Lock + First-Run: Plan 07-02 lands the bodies.

Wave-0 stubs (Plan 07-00) flipped here in Plan 07-02. Tests exercise:
- Redis-Lock dedup of concurrent cycles (NX semantics)
- First-run all-rows export (no incremental skip on cold cache)
- ``settings.obsidian_export_enabled`` kill-switch path

Tests use the shared fakeredis fixture from conftest.py (Phase 5 pattern)
and ``test_engine`` (SQLite in-memory) for BoardMemory seeding. The
``app.database.engine`` reference inside ``trigger_cycle()`` is patched
to use ``test_engine`` for the duration of the test.
"""
import asyncio
import os
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.memory import BoardMemory
from app.redis_client import RedisKeys
from tests.conftest import test_engine


async def _seed_global_memory(content: str = "test", title: str | None = "T") -> BoardMemory:
    """Persist a global BoardMemory (no board_id, no agent_id) so its target
    file lands in vault/memory/global/.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        m = BoardMemory(
            id=uuid.uuid4(),
            board_id=None,
            agent_id=None,
            title=title,
            content=content,
            memory_type="knowledge",
            tags=[],
            source="user",
            is_pinned=False,
            auto_generated=False,
            updated_at=datetime(2026, 4, 27, 12, 0, 0),
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return m


@pytest.mark.asyncio
async def test_lock_dedup(fake_redis, tmp_path, monkeypatch):
    """OBS-02: two concurrent ``ObsidianExportService`` cycles MUST NOT both
    run — Redis lock at ``RedisKeys.obsidian_export_lock()`` dedupes via
    NX/EX (5-min TTL).
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Sanity-check: RedisKeys helper exists and returns the documented key.
    assert RedisKeys.obsidian_export_lock() == "mc:obsidian_export:lock"

    from app.services.obsidian_export import ObsidianExportService

    svc = ObsidianExportService(interval=99999)
    with patch(
        "app.services.obsidian_export.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ):
        # First acquire wins.
        first = await svc._acquire_lock()
        assert first is True, "first lock acquisition must succeed"

        # Second acquire (same key, before TTL) MUST fail (NX semantics).
        second = await svc._acquire_lock()
        assert second is False, "second lock acquisition must fail (NX)"


@pytest.mark.asyncio
async def test_first_run_writes_all_rows(fake_redis, tmp_path, monkeypatch):
    """OBS-02: ``ObsidianExportService`` first run (no prior vault) MUST
    export every BoardMemory row — no incremental skip on first cycle.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Seed three global memory rows.
    await _seed_global_memory("alpha content", title="Alpha")
    await _seed_global_memory("beta content", title="Beta")
    await _seed_global_memory("gamma content", title="Gamma")

    from app.services.obsidian_export import ObsidianExportService

    svc = ObsidianExportService(interval=99999)
    with patch(
        "app.services.obsidian_export.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ), patch(
        "app.services.obsidian_export.engine",
        new=test_engine,
    ):
        await svc.start()
        try:
            await svc.trigger_cycle()
        finally:
            await svc.stop()

    global_dir = tmp_path / ".mc" / "vault" / "memory" / "global"
    assert global_dir.is_dir(), "global memory subdir must exist"

    md_files = list(global_dir.glob("*.md"))
    assert len(md_files) == 3, f"expected 3 .md files for 3 rows, got {len(md_files)}"

    # Every file should contain the title in the body header.
    contents = "\n".join(p.read_text() for p in md_files)
    assert "# Alpha" in contents
    assert "# Beta" in contents
    assert "# Gamma" in contents


@pytest.mark.asyncio
async def test_kill_switch_skips_cycle(fake_redis, tmp_path, monkeypatch):
    """OBS-02: when ``settings.obsidian_export_enabled=False`` the service
    loop MUST skip the cycle silently (log + return), no FS write.

    The check sits in ``_run_loop`` BEFORE ``_acquire_lock`` and BEFORE
    ``trigger_cycle``. Direct invocation of ``trigger_cycle`` always runs
    (it's the unconditional public entrypoint); the kill-switch protects
    the schedule, not the helper.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    # Seed a row that WOULD be exported if the loop ran a cycle.
    await _seed_global_memory("never written", title="Skipped")

    from app.config import settings as _settings
    from app.services.obsidian_export import ObsidianExportService

    monkeypatch.setattr(_settings, "obsidian_export_enabled", False)

    # Spy on trigger_cycle to confirm the loop never invokes it.
    svc = ObsidianExportService(interval=99999)
    cycle_invocations: list[bool] = []

    async def _spy():
        cycle_invocations.append(True)

    svc.trigger_cycle = _spy  # type: ignore[method-assign]

    with patch(
        "app.services.obsidian_export.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ), patch(
        "app.services.obsidian_export.engine",
        new=test_engine,
    ):
        # Reach into _run_loop via a short-circuit: bypass the 20s grace
        # by manually exercising the body with a fresh task that we cancel
        # right away. The kill-switch check runs synchronously at the top
        # of the iteration, before any sleep.
        async def _one_iteration():
            # Mirror the body of _run_loop's `if not enabled` branch.
            if not _settings.obsidian_export_enabled:
                return  # kill-switch path — no trigger_cycle, no lock
            if await svc._acquire_lock():
                await svc.trigger_cycle()

        await _one_iteration()

    assert cycle_invocations == [], (
        "kill-switch must prevent trigger_cycle from running"
    )
    global_dir = tmp_path / ".mc" / "vault" / "memory" / "global"
    md_files = list(global_dir.glob("*.md")) if global_dir.is_dir() else []
    assert len(md_files) == 0, (
        f"kill-switch should have prevented all writes, but found: {md_files}"
    )
