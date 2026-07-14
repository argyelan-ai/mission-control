"""Global Files API — portable, sandboxed browse/preview/download over ~/.mc.

All access goes through ``fs_service`` (one containment guard). File bytes
stream live from disk; the ``file_index`` only accelerates listing/search.
Native macOS "reveal in Finder" is an OPTIONAL, capability-detected bonus —
``native_open_available`` is per-entry and false whenever no host path exists
(Docker named volume) or the host helper is unreachable (mobile / Linux).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import Role, require_role, require_user
from app.database import get_session
from app.models.agent import Agent
from app.models.deliverable import TaskDeliverable
from app.models.file_index import FileIndexEntry
from app.models.task import Task
from app.services import file_indexer, fs_service, trash_service
from app.services.fs_roots import (
    RootBlocked,
    RootNotFound,
    browsable_roots,
    get_browsable_root,
    get_deletable_root,
)

logger = logging.getLogger("mc.files")

router = APIRouter(prefix="/api/v1/files", tags=["files"])


# --- native-open capability probe (cached 60s) -----------------------------

_probe_cache = {"at": 0.0, "ok": False}


async def _native_open_reachable() -> bool:
    """Is a native 'open' reveal possible from where this backend runs?

    Host macOS → yes (``open`` binary). In Docker → only if the host helper
    (mc-open-helper) is listening on host.docker.internal:8765. Cached 60s so
    ``/meta`` never pays the probe per request.
    """
    now = time.monotonic()
    if now - _probe_cache["at"] < 60:
        return bool(_probe_cache["ok"])

    in_docker = os.path.exists("/.dockerenv") or sys.platform.startswith("linux")
    if not in_docker:
        ok = sys.platform == "darwin"
    else:
        ok = False
        try:
            fut = asyncio.open_connection("host.docker.internal", 8765)
            _, writer = await asyncio.wait_for(fut, timeout=1.0)
            writer.close()
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
    _probe_cache.update(at=now, ok=ok)
    return ok


async def _reveal_on_host(host_path: str, *, reveal: bool) -> None:
    in_docker = os.path.exists("/.dockerenv") or sys.platform.startswith("linux")
    if in_docker:
        import httpx

        async with httpx.AsyncClient() as client:
            await client.post(
                "http://host.docker.internal:8765/open",
                json={"path": host_path, "reveal": reveal},
                timeout=3.0,
            )
    else:
        import subprocess

        subprocess.Popen(["open", "-R", host_path] if reveal else ["open", host_path])


def _entry_dict(e) -> dict:
    return asdict(e)


# Fields the frontend expects on EVERY /list entry (null unless resolved).
_READABILITY_NULLS = {"display_name": None, "agent_slug": None, "task_id": None}


async def _resolve_deliverable_task_dirs(
    entries, session: AsyncSession
) -> dict[str, dict]:
    """Map deliverables directory names that are task UUIDs → human-readable label.

    Only directory entries whose name parses as a UUID are candidates. All of
    them are resolved in ONE tasks query + ONE agents query (no N+1). A UUID
    whose task was deleted stays unresolved (caller emits all-null for it).
    """
    parsed: dict[str, uuid.UUID] = {}
    for e in entries:
        if not e.is_directory:
            continue
        try:
            parsed[e.name] = uuid.UUID(e.name)
        except (ValueError, AttributeError):
            continue
    if not parsed:
        return {}

    tasks = (
        await session.exec(select(Task).where(Task.id.in_(list(parsed.values()))))
    ).all()
    by_id = {t.id: t for t in tasks}

    agent_ids = {t.assigned_agent_id for t in tasks if t.assigned_agent_id is not None}
    agents: dict[uuid.UUID, Agent] = {}
    if agent_ids:
        rows = (await session.exec(select(Agent).where(Agent.id.in_(list(agent_ids))))).all()
        agents = {a.id: a for a in rows}

    resolved: dict[str, dict] = {}
    for name, tid in parsed.items():
        task = by_id.get(tid)
        if task is None:
            continue  # deleted task → stays null
        agent = agents.get(task.assigned_agent_id) if task.assigned_agent_id else None
        resolved[name] = {
            "display_name": task.title,
            "agent_slug": fs_service.agent_slug(agent),
            "task_id": str(tid),
        }
    return resolved


# --- endpoints (static segments only — no path params to order) ------------

@router.get("/roots")
async def list_roots(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    roots = []
    for r in browsable_roots():
        try:
            count = (
                await session.exec(
                    select(func.count()).select_from(FileIndexEntry).where(FileIndexEntry.root_key == r.key)
                )
            ).one()
        except Exception:  # noqa: BLE001 — index may be empty/missing
            count = 0
        roots.append(
            {
                "key": r.key,
                "label": r.label,
                "icon": r.icon,
                "native_open": r.native_open,
                "deletable": r.deletable,
                "indexed_count": int(count or 0),
            }
        )
    return {"roots": roots, "native_open_available": await _native_open_reachable()}


@router.get("/list")
async def list_files(
    root: str,
    subpath: str = "",
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    try:
        entries = fs_service.list_dir(root, subpath)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown or non-browsable root")
    except fs_service.FsAccessError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except fs_service.FsNotFound:
        raise HTTPException(status_code=404, detail="Not found")

    # Under the deliverables root, directory names are bare task UUIDs — resolve
    # them to human-readable task titles + owning agent slug (batched, no N+1).
    resolved: dict[str, dict] = {}
    if root == "deliverables":
        resolved = await _resolve_deliverable_task_dirs(entries, session)

    out = []
    for e in entries:
        out.append({**_entry_dict(e), **_READABILITY_NULLS, **resolved.get(e.name, {})})
    return {"root": root, "subpath": subpath, "entries": out}


# --- /search ?type= friendly-group mapping ---------------------------------
#
# The indexer stores whatever mimetypes.guess_type() returns, so many source
# files carry a NULL or generic mime (`.tsx`, `.rs`, `.go`, `.svelte`, …) and a
# raw mime substring match for "code"/"markdown" would silently return nothing.
# These groups therefore also match on filename extension. Extensionless code
# files (Dockerfile / Makefile) are matched by exact name.

_CODE_EXTS: tuple[str, ...] = (
    ".py", ".pyi", ".ipynb", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".rs", ".rb", ".java", ".kt", ".kts", ".scala", ".c", ".h", ".cpp",
    ".cc", ".cxx", ".hpp", ".hh", ".cs", ".php", ".swift", ".m", ".mm", ".sh",
    ".bash", ".zsh", ".fish", ".ps1", ".sql", ".json", ".jsonc", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".html", ".htm", ".xml", ".css", ".scss",
    ".sass", ".less", ".vue", ".svelte", ".lua", ".r", ".pl", ".pm", ".ex",
    ".exs", ".erl", ".clj", ".cljs", ".hs", ".ml", ".dart", ".groovy",
    ".gradle", ".proto", ".tf",
)
_CODE_EXACT_NAMES: tuple[str, ...] = ("Dockerfile", "Makefile", "Rakefile", "Gemfile")


def _type_filter(entry, type_str: str):
    """Translate a friendly ``?type=`` group into a SQL WHERE condition.

    ``image``/``video``/``audio`` → mime prefix; ``pdf`` → pdf mime or ``.pdf``;
    ``markdown`` → markdown mime or ``.md``/``.markdown``/``.mdx``; ``code`` →
    known source extensions (mime is unreliable for these). Any unrecognised
    value keeps the legacy raw-mime substring match (backward compatible).
    """
    t = type_str.strip().lower()
    if t in ("image", "video", "audio"):
        return entry.mime.ilike(f"{t}/%")
    if t == "pdf":
        return or_(entry.mime.ilike("%pdf%"), entry.name.ilike("%.pdf"))
    if t == "markdown":
        return or_(
            entry.mime.ilike("%markdown%"),
            entry.name.ilike("%.md"),
            entry.name.ilike("%.markdown"),
            entry.name.ilike("%.mdx"),
        )
    if t == "code":
        conds = [entry.name.ilike(f"%{ext}") for ext in _CODE_EXTS]
        conds += [entry.name.ilike(nm) for nm in _CODE_EXACT_NAMES]
        return or_(*conds)
    # Unknown group → legacy behaviour (raw mime substring).
    return entry.mime.ilike(f"%{type_str}%")


@router.get("/search")
async def search_files(
    q: str = "",
    type: str | None = None,
    agent: str | None = None,
    root: str | None = None,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    stmt = select(FileIndexEntry).where(FileIndexEntry.is_directory == False)  # noqa: E712
    if q:
        stmt = stmt.where(FileIndexEntry.name.ilike(f"%{q}%"))
    if root:
        stmt = stmt.where(FileIndexEntry.root_key == root)
    if agent:
        stmt = stmt.where(FileIndexEntry.agent_slug == agent)
    if type:
        stmt = stmt.where(_type_filter(FileIndexEntry, type))
    # Deterministic order so offset/limit paging is stable across page turns.
    capped = min(max(limit, 1), 500)
    stmt = (
        stmt.order_by(FileIndexEntry.name.asc(), FileIndexEntry.rel_path.asc())  # type: ignore[union-attr]
        .offset(max(offset, 0))
        .limit(capped + 1)  # fetch one extra to detect a next page
    )
    rows = (await session.exec(stmt)).all()
    has_more = len(rows) > capped
    rows = rows[:capped]
    return {
        "has_more": has_more,
        "results": [
            {
                "root": r.root_key,
                "rel_path": r.rel_path,
                "name": r.name,
                "size": r.size,
                "mime": r.mime,
                "mtime": r.mtime,
                "agent_slug": r.agent_slug,
                "task_id": str(r.task_id) if r.task_id else None,
            }
            for r in rows
        ]
    }


@router.get("/content")
async def get_content(
    root: str,
    subpath: str,
    download: bool = False,
    current_user=Depends(require_user),
):
    try:
        return fs_service.read_stream(root, subpath, download=download)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown or non-browsable root")
    except fs_service.FsAccessError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except fs_service.FsNotFound:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/meta")
async def get_meta(
    root: str,
    subpath: str = "",
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    try:
        root_obj = get_browsable_root(root)
        entry = fs_service.stat(root, subpath)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown or non-browsable root")
    except fs_service.FsAccessError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except fs_service.FsNotFound:
        raise HTTPException(status_code=404, detail="Not found")

    native = root_obj.native_open and await _native_open_reachable()
    idx = (
        await session.exec(
            select(FileIndexEntry).where(
                FileIndexEntry.root_key == root, FileIndexEntry.rel_path == subpath
            )
        )
    ).first()
    return {
        "root": root,
        "subpath": subpath,
        **_entry_dict(entry),
        "reachable": True,  # bytes always stream via /content
        "native_open_available": bool(native),
        "task_id": str(idx.task_id) if idx and idx.task_id else None,
        "deliverable_id": str(idx.deliverable_id) if idx and idx.deliverable_id else None,
        "agent_slug": idx.agent_slug if idx else None,
    }


class _OpenBody(BaseModel):
    root: str
    subpath: str = ""
    reveal: bool = True


@router.post("/open")
async def open_native(
    body: _OpenBody,
    current_user=Depends(require_user),
):
    try:
        root_obj = get_browsable_root(body.root)
        target = fs_service.safe_join(root_obj, body.subpath)  # containment check
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown or non-browsable root")
    except fs_service.FsAccessError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if root_obj.host_path is None:
        raise HTTPException(status_code=409, detail="Container-only location — download instead of reveal")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if not await _native_open_reachable():
        raise HTTPException(status_code=501, detail="Native open not available on this host (use download)")

    host_target = str(root_obj.host_path / body.subpath) if body.subpath else str(root_obj.host_path)
    await _reveal_on_host(host_target, reveal=body.reveal)
    return {"ok": True, "available": True}


@router.post("/reindex")
async def reindex(
    session: AsyncSession = Depends(get_session),
    current_user=Depends(require_user),
):
    result = await file_indexer.run_once(session)
    return result


# --- soft-delete (move to ~/.mc/.trash, never rm) --------------------------

class _DeleteBody(BaseModel):
    root: str
    subpaths: list[str]


MAX_BATCH = 200


def _norm_rel(root, sp: str) -> str:
    """Best-effort canonical rel for an already-vanished path, so a stale index
    row of a file gone from disk is still self-healed (idempotent retry)."""
    return os.path.normpath(sp.lstrip("/"))


async def _cascade(session, root_key, rel, skipped, label, *, reason) -> int:
    """Delete the file_index row(s) for ``rel`` under ``root_key`` and, where a
    row carries a deliverable_id, the matching TaskDeliverable — UNLESS that
    deliverable is referenced / reusable / pinned (cross-project safety).

    The vault/BoardMemory mirror is intentionally LEFT INTACT — deleting a file
    is not deleting a knowledge note. Returns the count of cascaded deliverables.
    """
    if reason:
        skipped.append({"root": root_key, "subpath": label, "reason": reason})
    # Gather file_index rows: exact + directory children, anchored to THIS root,
    # path-boundary safe (LIKE escaped so 'reports2' next to 'reports' is safe).
    esc = rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        await session.exec(
            select(FileIndexEntry).where(
                FileIndexEntry.root_key == root_key,
                (FileIndexEntry.rel_path == rel)
                | (FileIndexEntry.rel_path.like(f"{esc}/%", escape="\\")),
            )
        )
    ).all()
    deliverable_ids = {r.deliverable_id for r in rows if r.deliverable_id is not None}
    for r in rows:
        await session.delete(r)

    cascaded = 0
    from app.models.deliverable_reference import DeliverableReference

    for did in deliverable_ids:
        deliv = await session.get(TaskDeliverable, did)
        if deliv is None:
            continue
        # GUARD: never destroy a cross-project shared / reused / pinned deliverable.
        refs = (
            await session.exec(
                select(func.count())
                .select_from(DeliverableReference)
                .where(DeliverableReference.source_deliverable_id == did)
            )
        ).one()
        if refs or deliv.is_reusable or deliv.is_pinned:
            skipped.append(
                {"root": root_key, "subpath": label, "reason": "deliverable_kept_referenced"}
            )
            continue
        # NULL every file_index row still pointing at this deliverable — the FK
        # is bare NO-ACTION (migration 0129:40), so a dangling reference would
        # raise IntegrityError at commit, AFTER files already moved.
        await session.exec(
            update(FileIndexEntry)
            .where(FileIndexEntry.deliverable_id == did)
            .values(deliverable_id=None)
        )
        await session.delete(deliv)
        cascaded += 1
    return cascaded


@router.post("/delete")
async def delete_files(
    body: _DeleteBody,
    current_user=Depends(require_role(Role.OPERATOR)),  # viewer→403, unauth→401
    session: AsyncSession = Depends(get_session),
):
    # --- root policy (404 unknown / 403 blocked|sensitive) ---
    try:
        root = get_deletable_root(body.root)
    except RootNotFound:
        raise HTTPException(status_code=404, detail="Unknown root")
    except RootBlocked as e:
        raise HTTPException(status_code=403, detail=e.reason)

    if len(body.subpaths) > MAX_BATCH:
        raise HTTPException(status_code=422, detail=f"Too many subpaths (max {MAX_BATCH})")

    ts = trash_service.timestamp()
    raw = list(dict.fromkeys(body.subpaths))  # dedupe raw same-path-twice

    # === PHASE 1: validate ALL sources. Any containment violation = 400, MOVE NOTHING. ===
    planned: list[tuple[str, Path, str]] = []  # (raw_subpath, src, canonical_rel)
    skipped: list[dict] = []
    seen_rel: set[str] = set()
    for sp in raw:
        try:
            src, rel = trash_service.validate_source(root, sp)
        except fs_service.FsAccessError as e:
            raise HTTPException(
                status_code=400, detail=f"containment violation: {sp} ({e})"
            )  # hard abort, nothing moved
        except fs_service.FsNotFound:
            # already gone from disk — still self-heal a stale index row.
            await _cascade(
                session, root.key, _norm_rel(root, sp), skipped, sp, reason="not found"
            )
            continue
        if rel in seen_rel:
            continue
        seen_rel.add(rel)
        planned.append((sp, src, rel))

    # === PHASE 2a: DB cascade FIRST, flush to surface FK errors BEFORE moving. ===
    cascaded = 0
    for _, _src, rel in planned:
        cascaded += await _cascade(session, root.key, rel, skipped, rel, reason=None)
    await session.flush()  # FK violations raise HERE, before any file moves

    # === PHASE 2b: only now perform the moves. ===
    trashed: list[dict] = []
    moved: list[tuple[Path, Path]] = []
    try:
        for _, src, rel in planned:
            dest = trash_service.trash_one(root, src, rel, ts=ts)
            moved.append((src, dest))
            trashed.append({"root": root.key, "subpath": rel, "trash_path": str(dest)})
    except Exception:
        # Compensating rollback: move any already-moved files back, then abort.
        for s, d in moved:
            try:
                if d.exists() and not s.exists():
                    shutil.move(str(d), str(s))
            except Exception:  # noqa: BLE001 — best-effort restore
                logger.exception("failed to restore %s during delete rollback", d)
        await session.rollback()
        raise HTTPException(status_code=500, detail="delete failed; rolled back")

    await session.commit()
    return {"trashed": trashed, "skipped": skipped, "cascaded_deliverables": cascaded}


# --- trash list / restore / purge (the inverse of /delete) -----------------
#
# .trash is NOT a browsable FsRoot and must stay that way: these endpoints use
# trash_service._resolve_in_trash (the private .trash-base guard) for the SOURCE,
# never safe_join(some_root, ...). The generic /list + /content can never serve
# .trash because get_browsable_root raises KeyError for any unregistered key.


@router.get("/trash")
async def list_trash(
    current_user=Depends(require_role(Role.OPERATOR)),  # viewer→403, unauth→401
):
    """Bounded listing of soft-deleted files under ~/.mc/.trash."""
    return {"entries": trash_service.list_trash()}


class _TrashIdsBody(BaseModel):
    trash_ids: list[str]


@router.post("/trash/restore")
async def restore_trash(
    body: _TrashIdsBody,
    current_user=Depends(require_role(Role.OPERATOR)),
    session: AsyncSession = Depends(get_session),
):
    """Restore trashed files to their original (deletable) root + re-index them.

    Per-item best-effort (DIVERGENCE from /delete's all-or-nothing 400 abort):
    a forged id among many valid restores is returned in ``skipped``, NOT a hard
    400 — but SOURCE/DEST containment is still enforced per item, so this is a
    UX choice, not a security weakening.

    LIMITATION (v1): if a file's TaskDeliverable was cascade-deleted by /delete,
    restore brings the FILE back but NOT the DB row — the re-index creates a
    fresh file_index row with ``deliverable_id=None``.
    """
    import mimetypes

    if len(body.trash_ids) > MAX_BATCH:
        raise HTTPException(status_code=422, detail=f"Too many trash_ids (max {MAX_BATCH})")

    trash_ids = list(dict.fromkeys(body.trash_ids))  # dedupe

    # === PHASE 1: validate every id (source containment + parse + dest root). ===
    valid: list[str] = []
    skipped: list[dict] = []
    for tid in trash_ids:
        try:
            trash_service._resolve_in_trash(tid)  # source containment + symlink-safe
            _ts, root_key, _rel = trash_service.parse_trash_id(tid)
            get_deletable_root(root_key)  # writable-root gate
        except fs_service.FsAccessError:
            skipped.append({"trash_id": tid, "reason": "escape"})
            continue
        except fs_service.FsNotFound:
            skipped.append({"trash_id": tid, "reason": "not_found"})
            continue
        except RootNotFound:
            skipped.append({"trash_id": tid, "reason": "unknown_root"})
            continue
        except RootBlocked:
            skipped.append({"trash_id": tid, "reason": "blocked_root"})
            continue
        valid.append(tid)

    # === PHASE 2: move each valid file back + re-index. ===
    restored: list[dict] = []
    for tid in valid:
        try:
            root_key, dest_sub = trash_service.restore_one(tid)
        except fs_service.FsNotFound:
            skipped.append({"trash_id": tid, "reason": "not_found"})
            continue
        except (fs_service.FsAccessError, RootNotFound, RootBlocked):
            # Re-derived guard tripped between phases (TOCTOU / race) — skip.
            skipped.append({"trash_id": tid, "reason": "restore_failed"})
            continue
        except Exception:  # noqa: BLE001 — move failure, per-item best-effort
            logger.exception("restore_one failed for %s", tid)
            skipped.append({"trash_id": tid, "reason": "restore_failed"})
            continue

        # Re-index so the file reappears in /list + /search (mirror run_once).
        try:
            meta = fs_service.stat(root_key, dest_sub)
            await file_indexer._upsert(
                session,
                root_key,
                dest_sub,
                name=Path(dest_sub).name,
                is_directory=meta.is_directory,
                size=meta.size,
                mime=meta.mime if meta.mime is not None else mimetypes.guess_type(dest_sub)[0],
                mtime=meta.mtime,
            )
        except Exception:  # noqa: BLE001 — index is an accelerator; file is back
            logger.exception("re-index after restore failed for %s/%s", root_key, dest_sub)

        restored.append({"trash_id": tid, "root": root_key, "subpath": dest_sub})

    await session.commit()
    return {"restored": restored, "skipped": skipped}


@router.post("/trash/purge")
async def purge_trash(
    body: _TrashIdsBody,
    current_user=Depends(require_role(Role.OPERATOR)),
):
    """Hard-delete trashed files — IRREVERSIBLE, strictly confined to ~/.mc/.trash.

    No DB session: the file_index rows for trashed files were already removed by
    the original /delete cascade, and .trash is never walked/indexed.
    """
    if len(body.trash_ids) > MAX_BATCH:
        raise HTTPException(status_code=422, detail=f"Too many trash_ids (max {MAX_BATCH})")

    trash_ids = list(dict.fromkeys(body.trash_ids))

    # === PHASE 1: validate every id (containment + symlink). ===
    valid: list[str] = []
    skipped: list[dict] = []
    for tid in trash_ids:
        try:
            trash_service._resolve_in_trash(tid)
        except fs_service.FsAccessError:
            skipped.append({"trash_id": tid, "reason": "escape"})
            continue
        valid.append(tid)

    # === PHASE 2: the one intentional hard-delete, per valid id. ===
    purged: list[str] = []
    for tid in valid:
        try:
            trash_service.purge_one(tid)
        except fs_service.FsAccessError:
            skipped.append({"trash_id": tid, "reason": "escape"})
            continue
        except Exception:  # noqa: BLE001 — per-item best-effort
            logger.exception("purge_one failed for %s", tid)
            skipped.append({"trash_id": tid, "reason": "purge_failed"})
            continue
        purged.append(tid)

    return {"purged": purged, "skipped": skipped}
