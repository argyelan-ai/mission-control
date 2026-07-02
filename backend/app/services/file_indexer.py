"""File index population — capture-at-write + periodic background walk.

The ``file_index`` table is a listing/search accelerator. Two writers:

1. :func:`capture_deliverable` — called when a deliverable is registered, so
   size/mime/is_dir + a stable agent slug are recorded at write time (no
   runtime filesystem dependency to render the Files list, survives renames).
2. :class:`FileIndexer` — a singleton asyncio loop (Redis-locked, mirrors the
   intelligence service) that walks the browsable ``~/.mc`` roots and
   upserts/prunes entries on an interval.

Bytes never come from here — only listings. See ``fs_service`` for streaming.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from datetime import datetime
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import async_session_maker
from app.models.file_index import FileIndexEntry
from app.redis_client import get_redis
from app.services import fs_service
from app.services.fs_roots import browsable_roots, mc_home

logger = logging.getLogger("mc.file_indexer")

# Directories never worth indexing (huge / noise). Pruned during the walk.
SKIP_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "__pycache__", ".next", ".turbo", "dist", "build", ".trash"}
)


def _locate(host_path: str | None, container_path: str | None) -> tuple[str, str] | None:
    """Map a resolved deliverable path to a (root_key, rel_path) pair, or None."""
    if host_path:
        for r in browsable_roots():
            if r.host_path is None:
                continue
            base = str(r.host_path)
            if host_path == base:
                return r.key, ""
            if host_path.startswith(base + "/"):
                return r.key, host_path[len(base) + 1:]
    if container_path:
        for r in browsable_roots():
            if not r.container_override:
                continue
            base = str(r.container_path)
            if container_path == base:
                return r.key, ""
            if container_path.startswith(base + "/"):
                return r.key, container_path[len(base) + 1:]
    return None


async def _upsert(
    session: AsyncSession,
    root_key: str,
    rel_path: str,
    *,
    name: str,
    is_directory: bool,
    size: int,
    mime: str | None,
    mtime: float,
    agent_slug: str | None = None,
    runtime: str | None = None,
    task_id=None,
    deliverable_id=None,
) -> FileIndexEntry:
    existing = (
        await session.exec(
            select(FileIndexEntry).where(
                FileIndexEntry.root_key == root_key,
                FileIndexEntry.rel_path == rel_path,
            )
        )
    ).first()
    if existing is None:
        existing = FileIndexEntry(root_key=root_key, rel_path=rel_path, name=name)
        session.add(existing)
    existing.name = name
    existing.is_directory = is_directory
    existing.size = size
    existing.mime = mime
    existing.mtime = mtime
    if agent_slug is not None:
        existing.agent_slug = agent_slug
    if runtime is not None:
        existing.runtime = runtime
    if task_id is not None:
        existing.task_id = task_id
    if deliverable_id is not None:
        existing.deliverable_id = deliverable_id
    existing.indexed_at = datetime.utcnow()
    return existing


async def capture_deliverable(session: AsyncSession, deliverable, agent=None) -> FileIndexEntry | None:
    """Index one deliverable at registration time. No-op for URLs / inline text."""
    path = getattr(deliverable, "path", None)
    if not path or path.startswith(("http://", "https://")):
        return None

    container = await fs_service.resolve_deliverable(deliverable, session, target="container")
    if not container:
        return None
    host = await fs_service.resolve_deliverable(deliverable, session, target="host")
    loc = _locate(host, container)
    if loc is None:
        return None
    root_key, rel_path = loc

    # Best-effort stat (the file may legitimately not exist yet, e.g. sidecar
    # races). Fall back to zero/None metadata rather than failing the write.
    is_directory = False
    size = 0
    mime = None
    mtime = 0.0
    try:
        meta = fs_service.stat(root_key, rel_path)
        is_directory, size, mime, mtime = meta.is_directory, meta.size, meta.mime, meta.mtime
    except (fs_service.FsAccessError, fs_service.FsNotFound):
        mime = mimetypes.guess_type(path)[0]

    if agent is None and getattr(deliverable, "agent_id", None) is not None:
        agent = await fs_service._load_agent(deliverable, session)

    entry = await _upsert(
        session,
        root_key,
        rel_path,
        name=Path(rel_path).name or root_key,
        is_directory=is_directory,
        size=size,
        mime=mime,
        mtime=mtime,
        agent_slug=fs_service.agent_slug(agent),
        runtime=getattr(agent, "agent_runtime", None),
        task_id=getattr(deliverable, "task_id", None),
        deliverable_id=getattr(deliverable, "id", None),
    )
    return entry


async def run_once(session: AsyncSession, *, max_entries: int = 50_000) -> dict:
    """Walk the browsable roots, upsert entries, prune vanished ones."""
    seen: set[tuple[str, str]] = set()
    walked_roots: set[str] = set()
    count = 0

    for r in browsable_roots():
        base = r.container_path
        if not base.exists() or not base.is_dir():
            continue
        walked_roots.add(r.key)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for nm, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
                full = Path(dirpath) / nm
                try:
                    rel = str(full.relative_to(base))
                    st = full.stat()
                except (OSError, ValueError):
                    continue
                mime = None if is_dir else mimetypes.guess_type(nm)[0]
                await _upsert(
                    session, r.key, rel,
                    name=nm, is_directory=is_dir,
                    size=0 if is_dir else st.st_size,
                    mime=mime, mtime=st.st_mtime,
                )
                seen.add((r.key, rel))
                count += 1
                if count >= max_entries:
                    logger.warning("file_indexer hit max_entries=%d — index truncated", max_entries)
                    break

    # Prune entries for walked roots whose file no longer exists on disk.
    pruned = 0
    rows = (await session.exec(select(FileIndexEntry))).all()
    for row in rows:
        if row.root_key in walked_roots and (row.root_key, row.rel_path) not in seen:
            await session.delete(row)
            pruned += 1

    await session.commit()
    return {"indexed": count, "pruned": pruned, "roots": sorted(walked_roots)}


async def reusable_deliverables(session: AsyncSession, project_id=None) -> list:
    """Deliverables flagged is_reusable, plus those referenced into a project."""
    from app.models.deliverable import TaskDeliverable
    from app.models.deliverable_reference import DeliverableReference

    rows = list((await session.exec(select(TaskDeliverable).where(TaskDeliverable.is_reusable == True))).all())  # noqa: E712
    if project_id is not None:
        refs = (
            await session.exec(
                select(DeliverableReference).where(DeliverableReference.target_project_id == project_id)
            )
        ).all()
        ref_ids = {r.source_deliverable_id for r in refs if r.source_deliverable_id}
        if ref_ids:
            extra = (await session.exec(select(TaskDeliverable).where(TaskDeliverable.id.in_(ref_ids)))).all()
            rows = list({d.id: d for d in (rows + list(extra))}.values())
    return rows


class FileIndexer:
    """Singleton background walker. Redis-locked so multi-worker stays single-run."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(settings.file_index_interval)
                redis = await get_redis()
                # 1 worker only — short-TTL lock, mirrors intelligence dedup.
                got = await redis.set("mc:file-indexer:lock", "1", nx=True, ex=300)
                if not got:
                    continue
                async with async_session_maker() as session:
                    result = await run_once(session)
                logger.info("file_indexer walk: %s", result)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("file_indexer loop error")


file_indexer = FileIndexer()
