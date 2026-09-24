"""Scratch repos (docs/specs/head-launcher.md §9): local test repos the
operator listed in ``<heads_root>/scratch-repos``. They skip the GitHub
identity gate on the host.

A scratch repo whose clone's ``origin`` is a local bare repo under
``<heads_root>/scratch-origin/`` can never have a GitHub PR: there a pushed
branch plus a passed run record is the result. Same rules as
``scratch_origin()`` in scripts/head/mc-head (the host decides the sandbox
grant; the backend only needs them for the job text). ``~/.mc`` is mounted
1:1, so host paths are valid here too.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import unquote

from app.services.heads import paths


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
