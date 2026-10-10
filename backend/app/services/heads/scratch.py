"""Scratch repos (docs/specs/head-launcher.md §9): local test repos the
operator listed in ``<heads_root>/scratch-repos``. They skip the GitHub
identity gate on the host.

A scratch repo whose clone's ``origin`` is a local bare repo under
``<heads_root>/scratch-origin/`` can never have a GitHub PR: there a pushed
branch plus a passed run record is the result. Same rules as
``scratch_origin()`` in scripts/head/mc-head (the host decides the sandbox
grant; the backend only needs them for the job text). ``~/.mc`` is mounted
1:1, so host paths are valid here too.

A repos row with ``source="scratch"`` and ``full_name="scratch/<name>"`` is
prepared automatically (``plan``): the backend lists it in ``scratch-repos``
and, while neither clone nor bare origin exists, hands the row's ``url``
(where the code comes from) to mc-head as ``scratch_source``. mc-head builds
the origin and clone on the host — the container sees only ``~/.mc``, not
the operator's source path.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

from app.services.heads import paths

#: Repos row ``source`` for a scratch repo (ADR-050 registry, head launcher §9).
SOURCE_SCRATCH = "scratch"
#: Only these names are prepared automatically — the layout mc-head builds.
AUTO_NAME_RE = re.compile(r"^scratch/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})$")
#: Where a scratch repo may be built from. Same pattern as SCRATCH_SOURCE_RE
#: in scripts/head/mc-head (tests/test_heads_scratch_auto.py checks parity):
#: an absolute path or a git URL; no options, no ext::, no credentials, no
#: whitespace.
SOURCE_RE = re.compile(
    r"^(/[^\s\x00-\x1f\x7f]*"
    r"|file:///[^\s\x00-\x1f\x7f]*"
    r"|https://[^\s\x00-\x1f\x7f/@]+/[^\s\x00-\x1f\x7f]+"
    r"|ssh://([A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+(:[0-9]+)?/[^\s\x00-\x1f\x7f]+"
    r"|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+)$"
)


class ScratchSourceMissing(Exception):
    """A scratch repo needs preparing but its repos row has no usable source."""

    def __init__(self, full_name: str) -> None:
        super().__init__(f"scratch repo {full_name} has no source; add it under Repos")
        self.full_name = full_name


def scratch_repos() -> set[str]:
    try:
        text = (paths.heads_root() / "scratch-repos").read_text()
    except OSError:
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}


def is_scratch(full_name: str | None) -> bool:
    return bool(full_name) and full_name in scratch_repos()


def _origin_url(clone: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "config", "--file", str(clone / ".git" / "config"), "--no-includes",
             "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def local_origin(full_name: str | None) -> Path | None:
    """The local bare origin of a scratch repo, or None (every real repo)."""
    if not is_scratch(full_name):
        return None
    clone = paths.heads_root() / "clones" / full_name.replace("/", "--")
    url = _origin_url(clone)
    if url.startswith("file://"):
        url = unquote(url[len("file://"):])
    if not url.startswith("/"):
        return None
    try:
        root = (paths.heads_root() / "scratch-origin").resolve()
        path = Path(url).resolve()
    except (OSError, RuntimeError):
        return None
    if root not in path.parents or not path.is_dir():
        return None
    return path


def source_allowed(source: str | None) -> bool:
    """Same rule as ``scratch_source_allowed`` in scripts/head/mc-head."""
    if not isinstance(source, str) or len(source) > 1000 or not SOURCE_RE.match(source):
        return False
    if "/../" in source or source.endswith("/..") or "/./" in source:
        return False
    local = source[len("file://"):] if source.startswith("file://") else source
    if local.startswith("/"):
        try:
            path = Path(local).resolve()
            heads = paths.heads_root().resolve()
        except (OSError, RuntimeError):
            return False
        if path == heads or heads in path.parents:
            return False
    return True


def auto_name(full_name: str | None, source: str | None) -> str | None:
    """``<name>`` for a repos row that is prepared automatically, else None."""
    if source != SOURCE_SCRATCH or not full_name:
        return None
    m = AUTO_NAME_RE.match(full_name)
    return m.group(1) if m else None


def ensure_listed(full_name: str) -> None:
    """Add ``full_name`` to ``scratch-repos`` once; other lines stay as they are.

    One O_APPEND write: two starts at the same moment can at worst add the
    same line twice (the list is read as a set), never lose another line."""
    if full_name in scratch_repos():
        return
    target = paths.heads_root() / "scratch-repos"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = target.read_text()
    except FileNotFoundError:
        text = ""
    line = ("\n" if text and not text.endswith("\n") else "") + full_name + "\n"
    with target.open("a") as fh:
        fh.write(line)


def plan(full_name: str, source: str | None, url: str | None) -> tuple[str | None, bool]:
    """Prepare a scratch repo for a head run (blocking: file system only).

    Returns ``(scratch_source, local_origin_pending)``: the source mc-head
    builds the bare origin from (None when origin or clone already exist),
    and whether the run will have a local origin that does not exist yet
    (the job text then already says "push only"). Not an auto scratch repo
    → ``(None, False)``. Raises ScratchSourceMissing — before anything is
    written — when neither clone nor origin exists and ``url`` is unusable.
    """
    name = auto_name(full_name, source)
    if name is None:
        return None, False
    root = paths.heads_root()
    clone = root / "clones" / full_name.replace("/", "--")
    origin = root / "scratch-origin" / f"{name}.git"
    url = (url or "").strip()
    if (clone / ".git").exists():
        result: tuple[str | None, bool] = (None, False)
    elif origin.is_dir():
        result = (None, True)
    elif source_allowed(url):
        result = (url, True)
    else:
        raise ScratchSourceMissing(full_name)
    ensure_listed(full_name)
    return result
