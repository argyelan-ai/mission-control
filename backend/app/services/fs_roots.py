"""Single source of truth for the browsable MC filesystem roots.

Every operational file MC produces lives under the host's ``~/.mc/`` tree. The
backend container bind-mounts ``${HOME}/.mc:${HOME}/.mc`` 1:1, so for every
host-backed subtree the *container* path and the *host* path are identical
(``MC_HOME / <subtree>``). The one exception is the ``mc_shared_deliverables``
Docker named volume, mounted at ``/shared-deliverables`` with no host path —
browsable/streamable via the backend but never Finder-revealable.

This registry replaces the scattered ``HOME_HOST + "/.mc/..."`` string-building
spread across ~10 modules and the parallel write/read prefix lists in
``deliverable_paths.py`` + ``deliverable_fs_resolver.py``.

SECURITY: ``sensitive`` roots (``secrets``, agent config with tokens, browser
profiles, logs, backups) are NEVER returned by :func:`browsable_roots` and
:func:`get_browsable_root` refuses them — they must never be reachable through
the Files API.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import settings


def mc_home() -> Path:
    """Root of the MC home tree (``~/.mc``), host-equal via the 1:1 bind mount.

    NOTE: ``~/.mc/.trash`` (the soft-delete destination) is deliberately NOT a
    registered FsRoot, so :func:`browsable_roots` / :func:`get_browsable_root`
    can never list or serve it, and ``file_indexer.run_once`` never walks it
    (it walks :func:`browsable_roots` only). ``.trash`` is also in the indexer's
    SKIP_DIRS as belt-and-suspenders.
    """
    return Path(settings.home_host) / ".mc"


@dataclass(frozen=True)
class FsRoot:
    """A named, browsable (or explicitly sensitive) filesystem root."""

    key: str
    label: str
    icon: str  # lucide icon name hint consumed by the frontend
    subpath: str  # relative to mc_home() for host-backed roots
    native_open: bool  # a real host path exists → Finder-reveal is *possible*
    sensitive: bool  # never browsable through the Files API
    container_override: str | None = None  # absolute container path (named volume)
    deletable: bool = False  # trashable via Files /delete (host-backed, non-sensitive only)

    @property
    def container_path(self) -> Path:
        """The path the backend process can ``open()``/``scandir()``."""
        if self.container_override:
            return Path(self.container_override)
        return mc_home() / self.subpath

    @property
    def host_path(self) -> Path | None:
        """The path the operator's macOS Finder can reveal, or ``None`` if unaddressable."""
        if not self.native_open:
            return None
        return mc_home() / self.subpath


# Ordered registry. Browsable roots first (UI order), sensitive ones last.
_ROOTS: tuple[FsRoot, ...] = (
    FsRoot("deliverables", "Deliverables", "Package", "deliverables", True, False, deletable=True),
    FsRoot("workspaces", "Workspaces", "FolderGit2", "workspaces", True, False),
    FsRoot("vault", "Vault", "BookOpen", "vault", True, False),
    FsRoot("attachments", "Attachments", "Paperclip", "attachments", True, False),
    FsRoot("mcp-screenshots", "Screenshots", "Camera", "mcp-screenshots", True, False, deletable=True),
    FsRoot("media", "Media", "Image", "media", True, False, deletable=True),
    FsRoot("shared-artifacts", "Shared Artifacts", "Boxes", "shared-artifacts", True, False, deletable=True),
    FsRoot("storyboard-images", "Storyboards", "Clapperboard", "storyboard-images", True, False, deletable=True),
    # Docker named volume (mc-playwright sidecar) — browse/download only, no Finder.
    FsRoot(
        "shared-deliverables", "Sidecar Output", "HardDrive", "",
        native_open=False, sensitive=False, container_override="/shared-deliverables",
    ),
    # --- sensitive: registered so the guard is explicit, never browsable ---
    FsRoot("secrets", "Secrets", "Lock", "secrets", True, True),
    FsRoot("agents", "Agent Config", "Bot", "agents", True, True),  # token-bearing settings.json
    FsRoot("logs", "Logs", "ScrollText", "logs", True, True),
    FsRoot("backups", "Backups", "Archive", "backups", True, True),
    FsRoot("browser-profiles", "Browser Profiles", "Globe", "browser-profiles", True, True),
)

_BY_KEY: dict[str, FsRoot] = {r.key: r for r in _ROOTS}

# Exported for the indexer (walk these) and tests.
SENSITIVE_KEYS: frozenset[str] = frozenset(r.key for r in _ROOTS if r.sensitive)

# Roots whose files the Files /delete endpoint may soft-delete (move to .trash).
DELETABLE_KEYS: frozenset[str] = frozenset(r.key for r in _ROOTS if r.deletable)

# Import-time invariants (policy-by-assertion, not policy-by-omission):
# a fat-fingered deletable=True on vault/attachments must fail at import (CI).
assert DELETABLE_KEYS == {
    "deliverables", "media", "shared-artifacts", "mcp-screenshots", "storyboard-images",
}, f"DELETABLE_KEYS drifted from policy: {sorted(DELETABLE_KEYS)}"
assert not (SENSITIVE_KEYS & DELETABLE_KEYS), "a sensitive root must never be deletable"
assert DELETABLE_KEYS.isdisjoint(
    {"workspaces", "vault", "attachments", "shared-deliverables"}
), "a blocked root must never be deletable"
# every deletable root must be host-backed & same-fs (never a named volume) —
# cross-device shutil.move degrades to copy+unlink = real data loss.
for _k in DELETABLE_KEYS:
    assert _BY_KEY[_k].host_path is not None and _BY_KEY[_k].container_override is None, (
        f"deletable root {_k!r} must be host-backed with no container_override"
    )


class RootNotFound(Exception):
    """A truly unknown root key — Files /delete maps this to 404."""


class RootBlocked(Exception):
    """A registered but non-deletable root — Files /delete maps this to 403."""

    def __init__(self, key: str, reason: str):
        super().__init__(reason)
        self.key = key
        self.reason = reason


def get_deletable_root(key: str) -> FsRoot:
    """Resolve a root the Files /delete endpoint is allowed to soft-delete from.

    Raises :class:`RootNotFound` (→404) for unknown keys and :class:`RootBlocked`
    (→403, with a clear ``reason``) for sensitive / blocked / non-host-backed
    roots. The router NEVER touches ``_ROOTS`` / ``_BY_KEY`` / ``SENSITIVE_KEYS``
    directly — this is the single typed gate.
    """
    root = _BY_KEY.get(key)
    if root is None:
        raise RootNotFound(key)
    if root.sensitive:
        # Do NOT confirm existence to a probe beyond the generic 403 reason.
        raise RootBlocked(key, "Sensitive root — never deletable")
    if not root.deletable:
        reason = {
            "workspaces": "Agent git / in-flight tasks — never deletable",
            "vault": "Source of truth — edit via /memory",
            "attachments": "Knowledge-managed — never deletable",
            "shared-deliverables": "Docker named volume — never deletable",
        }.get(key, "This root is read-only")
        raise RootBlocked(key, reason)
    if root.host_path is None or root.container_override is not None:
        raise RootBlocked(key, "Not host-backed — refusing cross-volume move")
    return root


def browsable_roots() -> list[FsRoot]:
    """All roots the Files API may expose (sensitive roots filtered out)."""
    return [r for r in _ROOTS if not r.sensitive]


def get_browsable_root(key: str) -> FsRoot:
    """Look up a browsable root by key.

    Raises ``KeyError`` for unknown keys AND for sensitive roots, so the Files
    router can never be tricked into serving ``secrets`` or agent tokens.
    """
    root = _BY_KEY.get(key)
    if root is None or root.sensitive:
        raise KeyError(f"Unknown or non-browsable root: {key!r}")
    return root
