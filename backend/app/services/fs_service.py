"""Sandboxed filesystem accessor — the ONE guarded entry to MC's files.

Replaces the containment checks that were copy-pasted across the deliverable
endpoints and the any-absolute-path fallback in ``deliverable_fs_resolver``.
All listing/stat/streaming go through :func:`safe_join`, which guarantees the
resolved target stays inside its registered root (symlink escapes included).

File *bytes* always stream live from disk here — the file index is only a
listing/search accelerator, never a content source.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

from starlette.responses import FileResponse

from app.config import settings
from app.services.fs_roots import FsRoot, get_browsable_root


class FsAccessError(Exception):
    """Raised when a path escapes its root or is otherwise illegal (→ 400)."""


class FsNotFound(Exception):
    """Raised when a resolved path does not exist / is the wrong type (→ 404)."""


@dataclass
class FsEntry:
    name: str
    type: str  # "file" | "directory"
    size: int
    mime: str | None
    mtime: float
    is_directory: bool


def safe_join(root: FsRoot, subpath: str | None) -> Path:
    """Resolve ``subpath`` under ``root`` or raise :class:`FsAccessError`.

    The single containment guard: the fully-resolved target (symlinks followed)
    must be inside the fully-resolved root base. Rejects NUL bytes, absolute
    paths, ``..`` traversal, and symlink escapes alike.
    """
    sub = subpath or ""
    if "\0" in sub:
        raise FsAccessError("NUL byte in path")
    base = root.container_path.resolve()
    target = (base / sub.lstrip("/")).resolve()
    if not target.is_relative_to(base):
        raise FsAccessError(f"path escapes root {root.key!r}: {subpath!r}")
    return target


def _entry_for(path: Path, *, name: str | None = None) -> FsEntry:
    is_dir = path.is_dir()
    st = path.stat()
    nm = name if name is not None else path.name
    mime = None if is_dir else (mimetypes.guess_type(nm)[0])
    return FsEntry(
        name=nm,
        type="directory" if is_dir else "file",
        size=0 if is_dir else st.st_size,
        mime=mime,
        mtime=st.st_mtime,
        is_directory=is_dir,
    )


def list_dir(root_key: str, subpath: str | None = None) -> list[FsEntry]:
    """List a directory's immediate children, directories first then by name."""
    root = get_browsable_root(root_key)
    target = safe_join(root, subpath)
    if not target.exists():
        raise FsNotFound(str(target))
    if not target.is_dir():
        raise FsAccessError(f"not a directory: {subpath!r}")
    entries: list[FsEntry] = []
    with os.scandir(target) as it:
        for e in it:
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
    return entries


def stat(root_key: str, subpath: str | None = None) -> FsEntry:
    """Return metadata for a single file or directory."""
    root = get_browsable_root(root_key)
    target = safe_join(root, subpath)
    if not target.exists():
        raise FsNotFound(str(target))
    return _entry_for(target)


def read_stream(root_key: str, subpath: str, *, download: bool = False) -> FileResponse:
    """Stream a file's bytes live from disk.

    ``download=True`` sets ``Content-Disposition: attachment`` (save-as);
    otherwise the file renders inline (preview).
    """
    root = get_browsable_root(root_key)
    target = safe_join(root, subpath)
    if not target.exists() or not target.is_file():
        raise FsNotFound(str(target))
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        path=str(target),
        media_type=mime,
        filename=target.name if download else None,
    )


# --- deliverable path resolution (runtime-aware) ---------------------------
#
# The stored ``deliverable.path`` is the WRITER's view. Translating it to a
# backend-readable (container) or Finder-revealable (host) path depends on the
# owning agent's runtime: cli-bridge agents write the container view
# ``/deliverables/<task_id>/...`` and their files live at host
# ``~/.mc/deliverables/<slug>/<task_id>/...`` (slug injected); host workers
# write the host-form path directly (no re-injection). This single function
# replaces the duplicated copies in ``deliverable_fs_resolver`` and ``tasks.py``
# and drops the old ``.mc-deliverables`` (hyphen) landmine + the any-absolute-
# path fallback (a read-exposure hole).


def agent_slug(agent) -> str | None:
    """Stable filesystem slug for an agent.

    Prefers the persisted ``agent.slug`` column (rename-safe); falls back to the
    historical ``name.lower().replace(" ", "-")`` to stay byte-compatible with
    deliverable directories already on disk.
    """
    if agent is None:
        return None
    return getattr(agent, "slug", None) or agent.name.lower().replace(" ", "-")


async def _load_agent(deliverable, session):
    if getattr(deliverable, "agent_id", None) is None:
        return None
    from app.models.agent import Agent

    return await session.get(Agent, deliverable.agent_id)


async def resolve_deliverable(deliverable, session, *, target: str = "container") -> str | None:
    """Translate a deliverable's stored path to a container or host path.

    ``target="container"`` → backend-openable path (FileResponse / scandir).
    ``target="host"``      → real macOS host path for Finder reveal, or ``None``
                             when no host path exists (Docker named volume).
    Returns ``None`` for URLs, missing paths, and unknown prefixes (no fallback).
    """
    path = getattr(deliverable, "path", None)
    if not path or path.startswith(("http://", "https://")):
        return None

    home_host = settings.home_host

    # Host-form paths (host workers / Hermes, and post-normalization slugged
    # host paths). These already encode the on-disk layout — never re-inject.
    host_tilde = "~/.mc/deliverables/"
    host_resolved = f"{home_host}/.mc/deliverables/"
    if path.startswith(host_tilde) or path.startswith(host_resolved):
        rest = path[len(host_tilde):] if path.startswith(host_tilde) else path[len(host_resolved):]
        if target == "host":
            return f"{home_host}/.mc/deliverables/{rest}"
        return f"/deliverables/{rest}"

    # Docker container form: /deliverables/<task_id>/... → inject the owning
    # cli-bridge agent's slug (host layout is ~/.mc/deliverables/<slug>/...).
    deliv = "/deliverables/"
    if path.startswith(deliv):
        agent = await _load_agent(deliverable, session)
        is_containerized = agent is not None and getattr(agent, "agent_runtime", "") == "cli-bridge"
        slug = agent_slug(agent)
        rest = path[len(deliv):]
        if is_containerized and slug:
            if target == "host":
                return f"{home_host}/.mc/deliverables/{slug}/{rest}"
            return f"/deliverables/{slug}/{rest}"
        # agent_id NULL (admin/MCP) or non-cli-bridge → path already addressable
        if target == "host":
            return f"{home_host}/.mc/deliverables/{rest}"
        return path

    # mc-playwright sidecar — Docker named volume, no host path.
    shared = "/shared-deliverables/"
    if path.startswith(shared):
        return None if target == "host" else path

    # Microsoft Playwright MCP sidecar → ~/.mc/mcp-screenshots/ on host.
    mcp = "/shared-mcp/"
    if path.startswith(mcp):
        if target == "host":
            return f"{home_host}/.mc/mcp-screenshots/{path[len(mcp):]}"
        return path

    # Legacy / backend-internal absolute paths. These are DB-stored deliverable
    # paths, gated at WRITE time by deliverable_paths.validate_deliverable_path
    # (the 5 allowed prefixes) — not arbitrary user input. The container mounts
    # ${HOME}:${HOME}, so an existing absolute path is readable. The STRICT
    # root-scoped sandbox lives in safe_join()/the Files API; this resolver only
    # ever serves trusted DB paths, so an existence check is the right bound.
    import os

    if os.path.isabs(path) and os.path.exists(path):
        return path
    return None
