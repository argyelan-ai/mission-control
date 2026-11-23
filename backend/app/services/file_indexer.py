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
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
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
# Compared case-insensitively: the deployment FS is case-insensitive (APFS),
# so "Node_Modules" must prune like "node_modules" (mirrors
# task_workspace_files.py's case folding). Without this list the walk counts
# dependency/build/cache dumps and blows the per-root cap.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        # VCS / package managers
        ".git", "node_modules", "bower_components",
        # Python environments / bytecode / tooling caches
        ".venv", "venv", "__pycache__", "__pypackages__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".hypothesis",
        # JS framework build output
        ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
        # Generic build output
        "dist", "build", "out", "target", ".gradle",
        # Coverage / misc caches
        ".cache", "htmlcov", "coverage",
        # MC soft-delete
        ".trash",
    }
)
SKIP_DIR_SUFFIXES: tuple[str, ...] = (".egg-info",)


def _skip_dir(name: str) -> bool:
    n = name.lower()
    return n in SKIP_DIRS or n.endswith(SKIP_DIR_SUFFIXES)


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
    existing.indexed_at = datetime.now(timezone.utc)
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


async def capture_deliverable_id(
    deliverable_id,
    *,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> bool:
    """Index one freshly registered deliverable in its own short session.

    Called right after the deliverable-create endpoints answer (background
    task), so a new deliverable is searchable at once instead of after the
    next walk. Best-effort: never raises — the walk is the safety net.
    """
    from app.models.deliverable import TaskDeliverable

    factory = session_factory or async_session_maker
    try:
        async with factory() as session:
            deliverable = await session.get(TaskDeliverable, deliverable_id)
            if deliverable is None:
                return False
            entry = await capture_deliverable(session, deliverable)
            if entry is None:
                return False
            await session.commit()
            return True
    except Exception:  # noqa: BLE001 — best-effort, logged
        logger.warning("file_indexer capture failed for deliverable %s", deliverable_id, exc_info=True)
        return False


async def release_task_refs(session: AsyncSession, task_id) -> None:
    """Detach ``file_index`` rows from a task that is about to be deleted.

    ``file_index.task_id`` and ``file_index.deliverable_id`` are bare NO-ACTION
    foreign keys (migration 0129). Every path that deletes a task or its
    ``TaskDeliverable`` rows must call this BEFORE those deletes, or Postgres
    raises a ForeignKeyViolation at commit. The rows themselves stay: the
    files are still on disk and remain findable. Runs as immediate SQL, so it
    does not depend on the ORM flush order.
    """
    from app.models.deliverable import TaskDeliverable

    deliverable_ids = select(TaskDeliverable.id).where(TaskDeliverable.task_id == task_id)
    await session.exec(
        update(FileIndexEntry)
        .where(FileIndexEntry.deliverable_id.in_(deliverable_ids))  # type: ignore[union-attr]
        .values(deliverable_id=None)
    )
    await session.exec(
        update(FileIndexEntry).where(FileIndexEntry.task_id == task_id).values(task_id=None)
    )


# Per-root cap (entries walked per root and round). Live 2026-11-23 every
# indexed root holds < 3 000 entries; the cap only guards against a runaway
# tree. One root hitting it never starves the others any more.
DEFAULT_MAX_ENTRIES_PER_ROOT = 20_000

def _indexed_roots() -> list:
    """Roots the walk covers — browsable and ``indexed`` (not workspaces)."""
    return [r for r in browsable_roots() if r.indexed]


# Scanned entry: name, is_dir, size, mime, mtime
_Scan = tuple[str, bool, int, "str | None", float]


def _scan_root(base: Path, max_entries: int) -> tuple[dict[str, _Scan], bool]:
    """Walk one root (pruning SKIP_DIRS), stop at ``max_entries``."""
    entries: dict[str, _Scan] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        for nm, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
            full = Path(dirpath) / nm
            try:
                rel = str(full.relative_to(base))
                st = full.stat()
            except (OSError, ValueError):
                continue
            mime = None if is_dir else mimetypes.guess_type(nm)[0]
            entries[rel] = (nm, is_dir, 0 if is_dir else st.st_size, mime, st.st_mtime)
            if len(entries) >= max_entries:
                return entries, True
    return entries, False


async def run_once(
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES_PER_ROOT,
    batch_size: int = 1_000,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> dict:
    """Reconcile the index with the indexed roots on disk.

    * Walks :func:`indexed_roots` only — ``workspaces`` (code checkouts) is
      browsed live, never indexed.
    * ``max_entries`` caps each root separately, so one big root cannot eat
      the budget of the others.
    * Writes only what changed: new rows are inserted, rows whose
      size/mtime/kind differ are updated, rows whose file vanished are
      deleted. An unchanged round writes nothing.
    * Rows of roots that are no longer indexed or registered are pruned —
      except rows captured for a deliverable/task (provenance only the capture
      knows).

    The filesystem walk happens OUTSIDE any database transaction (incident
    2026-09-14: one 1789 s open transaction per round). Writes run in short
    committed batches. ``session_factory`` defaults to ``async_session_maker``;
    tests inject one bound to their engine.
    """
    factory = session_factory or async_session_maker
    t0 = time.monotonic()

    # --- Phase 1: scan the filesystem. NO session, NO transaction. ---------
    scans: dict[str, dict[str, _Scan]] = {}
    bases: dict[str, Path] = {}
    truncated_roots: set[str] = set()
    for r in _indexed_roots():
        base = r.container_path
        if not base.exists() or not base.is_dir():
            continue
        entries, truncated = _scan_root(base, max_entries)
        scans[r.key] = entries
        bases[r.key] = base
        if truncated:
            truncated_roots.add(r.key)
    walked_roots = set(scans)
    indexed_keys = {r.key for r in _indexed_roots()}

    # --- Phase 2: read the current rows of the walked roots (columns only) -
    existing: dict[tuple[str, str], tuple] = {}
    if walked_roots:
        async with factory() as session:
            rows = (
                await session.exec(
                    select(
                        FileIndexEntry.id,
                        FileIndexEntry.root_key,
                        FileIndexEntry.rel_path,
                        FileIndexEntry.is_directory,
                        FileIndexEntry.size,
                        FileIndexEntry.mtime,
                    ).where(FileIndexEntry.root_key.in_(walked_roots))  # type: ignore[union-attr]
                )
            ).all()
        for rid, root_key, rel, is_dir, size, mtime in rows:
            existing[(root_key, rel)] = (rid, bool(is_dir), int(size or 0), float(mtime or 0.0))

    # --- Phase 3: diff (pure, no DB) ---------------------------------------
    inserts: list[tuple[str, str, _Scan]] = []
    updates: list[dict] = []
    deletes: list = []
    for root_key, entries in scans.items():
        for rel, sc in entries.items():
            cur = existing.get((root_key, rel))
            if cur is None:
                inserts.append((root_key, rel, sc))
            elif (cur[1], cur[2], cur[3]) != (sc[1], sc[2], sc[4]):
                updates.append(
                    {"id": cur[0], "is_directory": sc[1], "size": sc[2], "mime": sc[3], "mtime": sc[4]}
                )
    for (root_key, rel), cur in existing.items():
        if rel in scans[root_key]:
            continue
        if root_key in truncated_roots:
            # Not reached because of the cap — delete only if really gone.
            if os.path.lexists(bases[root_key] / rel):
                continue
        deletes.append(cur[0])

    # --- Phase 4: write in short, committed batches -------------------------
    now = datetime.now(timezone.utc)
    for start in range(0, len(inserts), batch_size):
        chunk = inserts[start : start + batch_size]
        try:
            async with factory() as session:
                for root_key, rel, (nm, is_dir, size, mime, mtime) in chunk:
                    session.add(
                        FileIndexEntry(
                            root_key=root_key, rel_path=rel, name=nm, is_directory=is_dir,
                            size=size, mime=mime, mtime=mtime, indexed_at=now,
                        )
                    )
                await session.commit()
        except IntegrityError:
            # A capture inserted one of these rows meanwhile → upsert per row.
            async with factory() as session:
                for root_key, rel, (nm, is_dir, size, mime, mtime) in chunk:
                    await _upsert(
                        session, root_key, rel,
                        name=nm, is_directory=is_dir, size=size, mime=mime, mtime=mtime,
                    )
                await session.commit()
    for start in range(0, len(updates), batch_size):
        chunk = updates[start : start + batch_size]
        async with factory() as session:
            for u in chunk:
                await session.exec(
                    update(FileIndexEntry)
                    .where(FileIndexEntry.id == u["id"])
                    .values(
                        is_directory=u["is_directory"], size=u["size"], mime=u["mime"],
                        mtime=u["mtime"], indexed_at=now,
                    )
                )
            await session.commit()
    pruned = 0
    for start in range(0, len(deletes), batch_size):
        chunk = deletes[start : start + batch_size]
        async with factory() as session:
            await session.exec(delete(FileIndexEntry).where(FileIndexEntry.id.in_(chunk)))  # type: ignore[union-attr]
            await session.commit()
        pruned += len(chunk)

    # Rows of roots that are no longer indexed (workspaces) or no longer
    # registered at all (retired roots, never sensitive ones): walk leftovers
    # go, captured deliverable/task rows stay. One statement, no-op once clean.
    async with factory() as session:
        res = await session.exec(
            delete(FileIndexEntry).where(
                FileIndexEntry.root_key.not_in(indexed_keys),  # type: ignore[union-attr]
                FileIndexEntry.deliverable_id.is_(None),  # type: ignore[union-attr]
                FileIndexEntry.task_id.is_(None),  # type: ignore[union-attr]
            )
        )
        await session.commit()
        pruned_orphans = int(res.rowcount or 0)

    per_root = {k: len(v) for k, v in sorted(scans.items())}
    duration = round(time.monotonic() - t0, 3)
    logger.info(
        "file_indexer round: %.2fs per_root=[%s] inserted=%d updated=%d pruned=%d orphans=%d",
        duration,
        " ".join(f"{k}={n}" for k, n in per_root.items()),
        len(inserts), len(updates), pruned, pruned_orphans,
    )
    _log_truncation(max_entries, truncated_roots)
    return {
        "indexed": sum(per_root.values()),
        "per_root": per_root,
        "inserted": len(inserts),
        "updated": len(updates),
        "pruned": pruned + pruned_orphans,
        "roots": sorted(walked_roots),
        "truncated": bool(truncated_roots),
        "truncated_roots": sorted(truncated_roots),
        "duration_s": duration,
    }


# Once-per-state truncation warning: a run that stays truncated must not
# re-warn every round (the identical line every 600 s is invisible). Warn on
# entering the truncated state, log recovery once when it ends.
_truncated_last_run: bool = False


def _log_truncation(max_entries: int, truncated_roots: set[str]) -> None:
    global _truncated_last_run
    truncated = bool(truncated_roots)
    if truncated and not _truncated_last_run:
        logger.warning(
            "file_indexer hit max_entries=%d per root — index truncated "
            "(roots: %s); extend SKIP_DIRS or raise the cap",
            max_entries, ", ".join(sorted(truncated_roots)) or "-",
        )
    elif not truncated and _truncated_last_run:
        logger.info("file_indexer back under max_entries=%d per root", max_entries)
    _truncated_last_run = truncated


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
                await run_once()  # logs its own per-root line once per round
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("file_indexer loop error")


file_indexer = FileIndexer()
