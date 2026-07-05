"""Read-only Task-Workspace browsing — list/content over a task's ``workspace_path``.

Unlike the global Files API (``fs_service`` / ``fs_roots``), the root here is a
per-task DB value (``task.workspace_path``), not a fixed registry entry. This
module is the single security gate for it: it first proves the stored path
resolves inside the sandboxed ``workspaces`` FsRoot (``~/.mc/workspaces``)
before any ``scandir``/``open``, then applies a workspace-specific filter (dev
noise + credential-shaped files) that a task-workspace can accumulate but a
curated Files root wouldn't.

Bytes never come from here beyond ``resolve_workspace_file`` — the router
turns the resolved path into a ``FileResponse``, mirroring ``fs_service``.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from app.services.fs_roots import get_browsable_root
from app.services.fs_service import FsAccessError, FsEntry, FsNotFound

# Directories never listed or descended into — mirrors file_indexer.SKIP_DIRS
# plus workspace-specific noise (venv without the dot, .cache).
# The deployment filesystem (macOS/APFS) is case-insensitive, so all name
# comparisons below fold case — a request for ``.ENV`` or ``ID_RSA`` must be
# caught exactly like the lowercase form.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".trash", ".cache"}
)
_SKIP_DIR_NAMES_CF: frozenset[str] = frozenset(n.casefold() for n in SKIP_DIR_NAMES)

# Filename shapes that must never be listed or served, even inside an
# otherwise browsable workspace — credential material an agent may have
# written to disk while working (not a generic secrets root, so no registry
# guard catches these; this is the guard).
_SENSITIVE_SUFFIXES = (".pem", ".key")
_SENSITIVE_EXACT_NAMES: frozenset[str] = frozenset(n.casefold() for n in (".netrc", ".git-credentials"))


def _is_sensitive_name(name: str) -> bool:
    n = name.casefold()
    if n in _SENSITIVE_EXACT_NAMES:
        return True
    if n.startswith(".env"):
        return True
    if n.startswith("id_rsa"):
        return True
    return n.endswith(_SENSITIVE_SUFFIXES)


def _path_is_filtered(rel_path: str) -> bool:
    """True if any segment of ``rel_path`` is a skip-dir or a sensitive filename.

    Applied to the *requested subpath itself* (not just directory listings),
    so ``foo/.git/config`` 404s instead of leaking through a targeted request.
    """
    parts = [p for p in rel_path.split("/") if p not in ("", ".")]
    return any(part.casefold() in _SKIP_DIR_NAMES_CF or _is_sensitive_name(part) for part in parts)


def _contains_symlink(real_root: Path, sub: str) -> bool:
    """True if any segment from ``real_root`` down to ``sub`` is a symlink.

    Checked on the lexical (pre-``resolve()``) path so a symlink is caught
    before it's followed — a read-only browser has no need to traverse
    symlinks, and following one lets a name that passes ``_path_is_filtered``
    (e.g. ``harmless.txt``) point at a filtered target (e.g. ``.env``).
    """
    current = real_root
    for part in sub.split("/"):
        if not part or part == ".":
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def resolve_workspace_root(workspace_path: str | None) -> Path | None:
    """Real path of the task's workspace, or ``None`` if absent/escaped/gone.

    Defensive containment: even though ``workspace_path`` is a trusted DB
    column (not user input), it must resolve inside ``~/.mc/workspaces`` —
    this is the only thing standing between a corrupted/legacy row and an
    arbitrary host path becoming browsable.
    """
    if not workspace_path:
        return None
    root = get_browsable_root("workspaces")
    base = root.container_path.resolve()
    try:
        real = Path(workspace_path).resolve()
    except (OSError, RuntimeError):
        return None
    if not real.is_relative_to(base):
        return None
    if not real.exists() or not real.is_dir():
        return None
    return real


def list_workspace(workspace_path: str | None, subpath: str | None) -> tuple[bool, list[FsEntry]]:
    """List a subdirectory of the task's workspace.

    Returns ``(False, [])`` whenever the workspace itself is unavailable
    (``workspace_path`` unset, escaped, or the directory no longer exists) —
    never a 404, so the router can render a friendly empty state (200).

    Raises :class:`FsAccessError` for a traversal escape and :class:`FsNotFound`
    when ``subpath`` targets a filtered segment or a missing/non-directory path.
    """
    real_root = resolve_workspace_root(workspace_path)
    if real_root is None:
        return False, []

    sub = subpath or ""
    if "\0" in sub:
        raise FsAccessError("NUL byte in path")
    if _path_is_filtered(sub):
        raise FsNotFound(sub)
    if _contains_symlink(real_root, sub):
        raise FsNotFound(sub)
    target = (real_root / sub.lstrip("/")).resolve()
    if not target.is_relative_to(real_root):
        raise FsAccessError(f"path escapes workspace root: {subpath!r}")
    if _path_is_filtered(str(target.relative_to(real_root))):
        raise FsNotFound(sub)
    if not target.exists() or not target.is_dir():
        raise FsNotFound(str(target))

    entries: list[FsEntry] = []
    with os.scandir(target) as it:
        for e in it:
            if e.is_symlink():
                continue
            if e.name.casefold() in _SKIP_DIR_NAMES_CF or _is_sensitive_name(e.name):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            mime = None if is_dir else mimetypes.guess_type(e.name)[0]
            entries.append(
                FsEntry(
                    name=e.name,
                    type="directory" if is_dir else "file",
                    size=0 if is_dir else st.st_size,
                    mime=mime,
                    mtime=st.st_mtime,
                    is_directory=is_dir,
                )
            )
    entries.sort(key=lambda x: (not x.is_directory, x.name.lower()))
    return True, entries


def resolve_workspace_file(workspace_path: str | None, subpath: str) -> Path:
    """Resolve a single file inside the task's workspace.

    Raises :class:`FsNotFound` for an unavailable workspace, an empty/filtered
    subpath, or a missing/non-file target (all 404 at the router — a workspace
    or file that isn't there reads the same to a caller). Raises
    :class:`FsAccessError` for a traversal escape (400).
    """
    real_root = resolve_workspace_root(workspace_path)
    if real_root is None:
        raise FsNotFound("workspace unavailable")

    sub = subpath or ""
    if not sub:
        raise FsNotFound("empty subpath")
    if "\0" in sub:
        raise FsAccessError("NUL byte in path")
    if _path_is_filtered(sub):
        raise FsNotFound(sub)
    if _contains_symlink(real_root, sub):
        raise FsNotFound(sub)
    target = (real_root / sub.lstrip("/")).resolve()
    if not target.is_relative_to(real_root):
        raise FsAccessError(f"path escapes workspace root: {subpath!r}")
    if _path_is_filtered(str(target.relative_to(real_root))):
        raise FsNotFound(sub)
    if not target.exists() or not target.is_file():
        raise FsNotFound(str(target))
    return target
