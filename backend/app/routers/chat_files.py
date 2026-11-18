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
* The folder itself is resolved through symlinks and must STILL lie inside
  that boundary (``listing_root``): a session folder that is a link to ``/``
  or into ``~/.mc/secrets`` lists nothing.
* In a git repository the listing is ``git ls-files`` (tracked + untracked,
  ``.gitignore`` honoured — nested worktrees and ``.env`` stay out; git's
  ``core.fsmonitor`` hook is switched off, so a repo's config runs nothing).
  Elsewhere a breadth-first walk. Neither lists a symlink, ``.git``,
  ``node_modules``, virtualenvs, build output, nested agent worktrees or the
  MC home's sensitive folders; both stop at ``MAX_SCAN`` files (the walk also
  at ``MAX_DEPTH`` levels) and then answer ``truncated: true``.
* Operator only (``require_user``): an agent token cannot call it.
"""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import threading
from collections import deque
from concurrent.futures import Future
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
    ".worktrees",
})
#: Nested folders skipped by their path below a parent: the agent worktrees
#: Claude Code keeps in ``.claude/worktrees`` are full copies of the repo —
#: they would fill the listing with duplicates of every file.
IGNORED_NESTED = frozenset({(".claude", "worktrees")})
#: Most files one walk collects; beyond it the answer says ``truncated``.
MAX_SCAN = 20_000
#: Deepest folder level the walk enters below the working folder.
MAX_DEPTH = 12
#: Most matches one answer carries.
MAX_LIMIT = 100
#: How long one walk's result serves further keystrokes for the same folder.
CACHE_SECONDS = 15.0
#: Longest ``git ls-files`` may take before the plain walk takes over.
GIT_TIMEOUT_SECONDS = 10.0

_cache: dict[str, tuple[float, list[str], bool]] = {}
#: Walks running right now, per folder: parallel keystrokes wait for the one
#: walk instead of starting their own (single flight).
_inflight: dict[str, Future] = {}
_cache_lock = threading.Lock()


def _blocked_dirs() -> frozenset[Path]:
    """The MC home's sensitive folders (secrets, agent tokens, logs,
    backups, browser profiles) — never entered, even when a host chat works
    in the MC home itself."""
    mc_home = workspace_diff._host_home() / ".mc"
    homes = {mc_home}
    try:
        homes.add(mc_home.resolve())
    except OSError:
        pass
    return frozenset(home / name for home in homes for name in sensitive_subpaths())


def _skipped_dir(parts: tuple[str, ...]) -> bool:
    """A folder (as path parts below the listed folder) never listed into."""
    if parts and parts[-1] in IGNORED_DIRS:
        return True
    return len(parts) >= 2 and (parts[-2], parts[-1]) in IGNORED_NESTED


def walk_files(root: Path, *, max_scan: int | None = None,
               max_depth: int | None = None) -> tuple[list[str], bool]:
    """Every file below ``root`` as a POSIX path relative to it, and whether
    the listing stopped early. Synchronous — run it via ``to_thread``.

    A git repository is listed by git (``.gitignore`` holds), anything else
    by a breadth-first walk. Symlinks are never followed nor listed: a link
    can point anywhere, a listing is only about what lies in this folder."""
    cap = MAX_SCAN if max_scan is None else max_scan
    listed = _git_files(root, cap)
    if listed is not None:
        return listed
    return _walk(root, cap, MAX_DEPTH if max_depth is None else max_depth)


def _git_files(root: Path, cap: int) -> tuple[list[str], bool] | None:
    """``git ls-files`` below ``root`` (tracked + untracked, ignored ones
    out), filtered by the same rules as the walk. ``None``: not a git
    repository (or git failed) — the walk takes over."""
    try:
        proc = subprocess.run(
            ["git", "--no-pager", "-c", "safe.directory=*", "-c", "core.fsmonitor=false",
             "-c", "core.quotepath=false", "-C", str(root),
             "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    blocked = _blocked_dirs()
    files: list[str] = []
    seen: set[str] = set()
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode("utf-8", errors="replace")
        # "dir/" = a nested repository (not a file); a path listed twice is
        # a file with a merge conflict.
        if not rel or rel.endswith("/") or rel in seen:
            continue
        parts = tuple(rel.split("/"))
        if any(_skipped_dir(parts[: i + 1]) for i in range(len(parts) - 1)):
            continue
        path = root / rel
        if blocked and any(parent in blocked for parent in path.parents):
            continue
        try:
            # Only real files: no symlink, and nothing git lists that is gone.
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
        except OSError:
            continue
        if len(files) >= cap:
            return files, True
        seen.add(rel)
        files.append(rel)
    return files, False


def _walk(root: Path, cap: int, depth_cap: int) -> tuple[list[str], bool]:
    """Breadth first: every level is listed before the next one, so one huge
    folder cannot eat the whole budget before its siblings are reached."""
    blocked = _blocked_dirs()
    files: list[str] = []
    truncated = False
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        folder, depth = queue.popleft()
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
                    if sub in blocked or _skipped_dir(sub.relative_to(root).parts):
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
        queue.extend((s, depth + 1) for s in subdirs)
    return files, truncated


def _cached_walk(root: Path) -> tuple[list[str], bool]:
    """One listing per folder serves keystrokes for ``CACHE_SECONDS``.
    Keystrokes during a cold listing wait for it (single flight); expired
    folders are dropped, so the cache never grows past the folders in use."""
    key = str(root)
    now = time.monotonic()
    with _cache_lock:
        for stale in [k for k, v in _cache.items() if now - v[0] >= CACHE_SECONDS]:
            del _cache[stale]
        hit = _cache.get(key)
        if hit is not None:
            return hit[1], hit[2]
        running = _inflight.get(key)
        owner = running is None
        if owner:
            running = _inflight[key] = Future()
    if not owner:
        return running.result()
    try:
        files, truncated = walk_files(root)
    except BaseException as exc:
        with _cache_lock:
            _inflight.pop(key, None)
        running.set_exception(exc)
        raise
    with _cache_lock:
        _cache[key] = (now, files, truncated)
        _inflight.pop(key, None)
    running.set_result((files, truncated))
    return files, truncated


def _score(path: str, query: str) -> tuple[int, int] | None:
    """How well ``path`` matches ``query`` (lower is better), or ``None``.

    0 file name equals the query · 1 file name starts with it · 2 file name
    contains it · 3 the path contains it · 4 the query's characters appear
    in order in the FILE NAME (``cmpsr`` finds ``Composer.tsx``; over the
    whole path almost any word would match something). The second value is
    where the match starts in its tier, so an earlier hit wins."""
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
        pos = name.find(ch, pos + 1)
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


def _inside(path: Path, boundary: Path) -> bool:
    return path == boundary or boundary in path.parents


def _allowed_resolved(folders: _ChatFolders, resolved: Path) -> bool:
    """The resolved folder still lies where ``host_path_for_agent_cwd`` would
    let it: inside the Docker agent's own workspace, or inside the MC home
    but outside its sensitive folders (host agents)."""
    if folders.runtime == "cli-bridge":
        if folders.root is None:
            return False
        try:
            return _inside(resolved, folders.root.resolve())
        except OSError:
            return False
    mc_home = workspace_diff._host_home() / ".mc"
    try:
        mc_real = mc_home.resolve()
    except OSError:
        return False
    if not _inside(resolved, mc_real):
        return False
    rel = resolved.relative_to(mc_real).parts
    return not (rel and rel[0] in sensitive_subpaths())


def listing_root(folders: _ChatFolders) -> Path | None:
    """The folder whose files the chat may mention: the session's own folder,
    else (no folder recorded at all) the agent's workspace — through the
    same guard a recorded folder passes. ``None``: nothing to list.

    The guard checks the path as recorded; a symlink on the way could still
    lead out (``/workspace/x -> /``, a session folder linked into
    ``~/.mc/secrets``). So the RESOLVED folder is checked once more."""
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
    if not _allowed_resolved(folders, resolved):
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
