"""Files of the folder a chat works in — the source of the composer's ``@``
file mention.

``GET /api/v1/agents/{agent_id}/chat/files?q=…`` answers with paths
RELATIVE to the chat's working folder, best match first. The composer puts
the chosen path into the draft as ``@<path>``; the harness reads the file
itself (it works in that same folder). MC uploads, copies or attaches
nothing — this endpoint only names files.

Which folder: the one the chat session itself works in (the same
``_chat_repo_picker`` facts the diff and Changes panel use). Without a
recorded session folder, the agent's workspace. Never anything else.

Safety, by construction rather than by checks on user input:

* The request carries **no path at all** — only a search text that is
  matched against names. There is nothing to traverse with.
* The folder comes from ``host_path_for_agent_cwd`` (Docker agents: only
  inside their own workspace; host agents: only inside the MC home, never
  its sensitive folders). A host agent's fallback workspace goes through the
  same guard.
* The walk never follows a symlink (a link to ``/`` lists nothing), skips
  ``.git``, ``node_modules``, virtualenvs and build output, never enters the
  MC home's sensitive folders, and stops at ``MAX_SCAN`` files and
  ``MAX_DEPTH`` levels (``truncated: true`` then).
* Operator only (``require_user``): an agent token cannot call it.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import require_user
from app.database import get_session
from app.routers.agent_chat import _ChatFolders, _chat_repo_picker, _load_agent_or_404
from app.services import workspace_diff
from app.services.fs_roots import sensitive_subpaths

router = APIRouter(prefix="/api/v1", tags=["agent-chat"])

_NO_WORKSPACE = {"reason": "no_workspace"}

#: Folders never walked into: version control, dependencies, virtualenvs,
#: caches and build output. Their files are never what one mentions.
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next", ".turbo",
    ".cache", "dist", "build", "coverage", ".gradle", "target", ".idea",
})
#: Most files one walk collects; beyond it the answer says ``truncated``.
MAX_SCAN = 20_000
#: Deepest folder level the walk enters below the working folder.
MAX_DEPTH = 12
#: Most matches one answer carries.
MAX_LIMIT = 100
#: How long one walk's result serves further keystrokes for the same folder.
CACHE_SECONDS = 15.0

_cache: dict[str, tuple[float, list[str], bool]] = {}
_cache_lock = threading.Lock()


def _blocked_dirs() -> frozenset[Path]:
    """The MC home's sensitive folders (secrets, agent tokens, logs,
    backups, browser profiles) — never entered, even when a host chat works
    in the MC home itself."""
    mc_home = workspace_diff._host_home() / ".mc"
    return frozenset(mc_home / name for name in sensitive_subpaths())


def walk_files(root: Path, *, max_scan: int | None = None,
               max_depth: int | None = None) -> tuple[list[str], bool]:
    """Every file below ``root`` as a POSIX path relative to it, and whether
    the walk stopped early. Synchronous — run it via ``to_thread``.

    Symlinks are never followed nor listed: a link can point anywhere, a
    listing is only about what lies in this folder."""
    cap = MAX_SCAN if max_scan is None else max_scan
    depth_cap = MAX_DEPTH if max_depth is None else max_depth
    blocked = _blocked_dirs()
    files: list[str] = []
    truncated = False
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        subdirs: list[Path] = []
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    sub = Path(entry.path)
                    if entry.name in IGNORED_DIRS or sub in blocked:
                        continue
                    if depth + 1 > depth_cap:
                        truncated = True
                        continue
                    subdirs.append(sub)
                elif entry.is_file(follow_symlinks=False):
                    if len(files) >= cap:
                        return files, True
                    files.append(Path(entry.path).relative_to(root).as_posix())
            except OSError:
                continue
        # Reverse: the stack pops the alphabetically first folder next.
        stack.extend((s, depth + 1) for s in reversed(subdirs))
    return files, truncated


def _cached_walk(root: Path) -> tuple[list[str], bool]:
    key = str(root)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
    files, truncated = walk_files(root)
    with _cache_lock:
        _cache[key] = (now, files, truncated)
    return files, truncated


def _score(path: str, query: str) -> tuple[int, int] | None:
    """How well ``path`` matches ``query`` (lower is better), or ``None``.

    0 file name equals the query · 1 file name starts with it · 2 file name
    contains it · 3 the path contains it · 4 the query's characters appear
    in order in the path (``cmpsr`` finds ``Composer.tsx``). The second value
    is where the match starts in its tier, so an earlier hit wins."""
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    if name == query:
        return (0, 0)
    if name.startswith(query):
        return (1, 0)
    at = name.find(query)
    if at >= 0:
        return (2, at)
    at = p.find(query)
    if at >= 0:
        return (3, at)
    pos = -1
    first = -1
    for ch in query:
        pos = p.find(ch, pos + 1)
        if pos < 0:
            return None
        if first < 0:
            first = pos
    return (4, first)


def rank(files: list[str], query: str, limit: int) -> list[str]:
    """The best ``limit`` matches. An empty query lists the shallowest files
    first — what sits right in the folder is what one mentions most."""
    q = query.strip().lower()
    if not q:
        return sorted(files, key=lambda f: (f.count("/"), f.lower()))[:limit]
    scored = []
    for f in files:
        s = _score(f, q)
        if s is not None:
            scored.append((s, len(f), f.lower(), f))
    scored.sort()
    return [row[3] for row in scored[:limit]]


def listing_root(folders: _ChatFolders) -> Path | None:
    """The folder whose files the chat may mention: the session's own folder,
    else (no folder recorded at all) the agent's workspace — through the
    same guard a recorded folder passes. ``None``: nothing to list."""
    if folders.cwd is not None:
        candidate = folders.session_dir
    elif folders.root is None:
        candidate = None
    elif folders.runtime == "cli-bridge":
        candidate = folders.root
    else:
        candidate = workspace_diff.host_path_for_agent_cwd(
            folders.runtime, folders.root, str(folders.root))
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


@router.get("/agents/{agent_id}/chat/files")
async def get_chat_files(
    agent_id: uuid.UUID,
    q: str = Query("", max_length=200),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    current_user=Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Files of the chat's working folder matching ``q`` (fuzzy, best
    first): ``{"root": <folder as the agent names it>, "files": [{"path",
    "name"}], "truncated": bool}``. 404 ``{"reason": "no_workspace"}`` when
    the chat has no readable folder. Read-only."""
    agent = await _load_agent_or_404(agent_id, session)
    folders = await _chat_repo_picker(agent, session)

    def _list() -> dict[str, Any] | None:
        root = listing_root(folders)
        if root is None:
            return None
        files, truncated = _cached_walk(root)
        hits = rank(files, q, limit)
        return {
            "root": workspace_diff.display_path(folders.runtime, folders.root, root),
            "files": [{"path": p, "name": p.rsplit("/", 1)[-1]} for p in hits],
            "truncated": truncated,
        }

    result = await asyncio.to_thread(_list)
    if result is None:
        return JSONResponse(status_code=404, content=_NO_WORKSPACE)
    return result
