"""Structured git diff over an agent's workspace — powers the diff panel in
Sessions Chat.

Two scopes:
  * ``"worktree"``    — uncommitted changes (staged + unstaged) against HEAD,
                          via ``git diff HEAD``.
  * ``"last-commit"``  — the most recent commit, via ``git show HEAD``.

Returns the same shape as ``GitService.get_commit_diff`` (frontend
``types.ts:158-184`` — ``CommitDiff``/``CommitDiffFile``/``CommitDiffHunk``/
``CommitDiffLine``), but is deliberately independent of ``GitService``:
that class's ``_run_cmd`` calls ``_ensure_git_auth`` on every invocation
(vault lookup + rewriting the global git credential store for GitHub push
auth) — unwanted overhead and a side effect for a read-only local diff that
the chat UI may poll repeatedly.

Stats (``additions``/``deletions`` per file and in aggregate) come from
``git diff --numstat``, kept separate from the unified-diff hunk parse, so
per-file counts stay accurate even when a file's hunk lines are truncated
by the ``_MAX_LINES_PER_FILE`` cap below.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from app.services.token_harvester import _host_home

logger = logging.getLogger("mc.workspace_diff")

_MAX_FILES = 200
_MAX_LINES_PER_FILE = 5000

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


class NoWorkspaceError(Exception):
    """Raised when the agent has no usable git workspace on disk — caller
    (router) maps this to 404 ``{"reason": "no_workspace"}``."""


def resolve_workspace_path(raw: str) -> Path:
    """Resolves ``agent.workspace_path`` (DB) to the path this backend
    process can read.

    Since ADR-022, the DB stores absolute HOST paths already (verified
    against live rows: e.g. ``/Users/<host-user>/.mc/workspaces/rex`` — no ``~``
    prefix in practice). The backend container mounts ``${HOME}:${HOME}``
    1:1 (see CLAUDE.local.md), so that host path resolves unchanged
    in-container. Only legacy/hand-edited rows that do carry a literal
    ``~`` need the ``token_harvester._host_home()`` translation — mirrors
    ``token_harvester._expand_harvest_path``.
    """
    if raw.startswith("~"):
        return _host_home() / raw.lstrip("~/").lstrip("/")
    return Path(raw)


def _run_git(*args: str, cwd: Path) -> str:
    """Runs git with an argv list (never a shell string) and returns
    stdout. ``-c safe.directory=*`` guards against git's dubious-ownership
    refusal for bind-mounted agent workspaces; ``-c core.quotepath=false``
    keeps umlaut/unicode filenames literal instead of octal-escaped."""
    result = subprocess.run(
        [
            "git", "--no-pager",
            "-c", "safe.directory=*",
            "-c", "core.quotepath=false",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise NoWorkspaceError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout


def _parse_numstat(numstat_raw: str) -> list[dict[str, Any]]:
    """Parses ``git diff --numstat`` / ``git show --numstat`` output into
    ``{filename, additions, deletions, hunks: []}`` dicts (hunks filled in
    later by ``_merge_hunks``). Binary files (``-\t-\tpath``) get 0/0."""
    files: list[dict[str, Any]] = []
    for line in numstat_raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        add_s, del_s, filename = parts
        additions = 0 if add_s == "-" else int(add_s)
        deletions = 0 if del_s == "-" else int(del_s)
        files.append(
            {"filename": filename, "additions": additions, "deletions": deletions, "hunks": []}
        )
    return files


def _parse_unified_diff(diff_raw: str) -> dict[str, list[dict[str, Any]]]:
    """Parses unified ``git diff``/``git show`` output into
    ``{filename: [hunk, ...]}``. Caps at ``_MAX_FILES`` files and
    ``_MAX_LINES_PER_FILE`` diff lines per file — the remainder of an
    over-cap file is replaced by a synthetic ``"… truncated"`` ctx line.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    current_filename: str | None = None
    current_hunks: list[dict[str, Any]] | None = None
    current_hunk: dict[str, Any] | None = None
    old_line = 0
    new_line = 0
    file_line_count = 0
    file_truncated = False
    files_seen = 0

    def _flush_hunk() -> None:
        nonlocal current_hunk
        if current_hunk is not None and current_hunks is not None:
            current_hunks.append(current_hunk)
            current_hunk = None

    def _flush_file() -> None:
        if current_filename is not None and current_hunks is not None:
            result[current_filename] = current_hunks

    for raw_line in diff_raw.splitlines():
        if raw_line.startswith("diff --git "):
            if files_seen >= _MAX_FILES:
                break
            _flush_hunk()
            _flush_file()
            files_seen += 1
            m = _DIFF_GIT_RE.match(raw_line)
            current_filename = m.group(2) if m else ""
            current_hunks = []
            file_line_count = 0
            file_truncated = False
            continue

        if current_hunks is None:
            continue

        if raw_line.startswith("+++ b/"):
            current_filename = raw_line[6:]
            continue
        if raw_line.startswith("+++ /dev/null"):
            current_filename = current_filename or "(deleted)"
            continue
        if raw_line.startswith((
            "--- ", "index ", "new file", "deleted file", "Binary files",
            "similarity index", "rename from", "rename to", "\\",
        )):
            continue

        if raw_line.startswith("@@ "):
            _flush_hunk()
            m = _HUNK_HEADER_RE.match(raw_line)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(2))
            current_hunk = {"header": raw_line, "lines": []}
            continue

        if current_hunk is None:
            continue
        if file_truncated:
            continue
        if file_line_count >= _MAX_LINES_PER_FILE:
            current_hunk["lines"].append(
                {"type": "ctx", "content": "… truncated", "old_no": None, "new_no": None}
            )
            file_truncated = True
            continue

        if raw_line.startswith("+"):
            current_hunk["lines"].append(
                {"type": "add", "content": raw_line[1:], "old_no": None, "new_no": new_line}
            )
            new_line += 1
        elif raw_line.startswith("-"):
            current_hunk["lines"].append(
                {"type": "del", "content": raw_line[1:], "old_no": old_line, "new_no": None}
            )
            old_line += 1
        elif raw_line.startswith(" "):
            current_hunk["lines"].append(
                {"type": "ctx", "content": raw_line[1:], "old_no": old_line, "new_no": new_line}
            )
            old_line += 1
            new_line += 1
        else:
            continue
        file_line_count += 1

    _flush_hunk()
    _flush_file()
    return result


def _merge_hunks(
    numstat_files: list[dict[str, Any]], hunks_by_filename: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    for f in numstat_files:
        f["hunks"] = hunks_by_filename.get(f["filename"], [])
    return numstat_files


def _build_result(
    *, hash: str, message: str, author: str, date: str, files: list[dict[str, Any]]
) -> dict[str, Any]:
    files = files[:_MAX_FILES]
    return {
        "hash": hash,
        "message": message,
        "author": author,
        "date": date,
        "stats": {
            "files": len(files),
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
        },
        "files": files,
    }


def _worktree_diff(workspace: Path) -> dict[str, Any]:
    numstat_raw = _run_git("diff", "HEAD", "--numstat", cwd=workspace)
    diff_raw = _run_git("diff", "HEAD", "--unified=3", "--no-color", cwd=workspace)
    files = _merge_hunks(_parse_numstat(numstat_raw), _parse_unified_diff(diff_raw))
    return _build_result(hash="", message="Uncommitted changes", author="", date="", files=files)


def _commit_diff(workspace: Path, commit: str) -> dict[str, Any]:
    meta_raw = _run_git(
        "log", "-1", "--pretty=format:%h\x1f%s\x1f%an\x1f%ar", commit, cwd=workspace
    )
    parts = meta_raw.split("\x1f", 3)
    commit_hash = parts[0] if len(parts) > 0 else commit[:7]
    message = parts[1] if len(parts) > 1 else ""
    author = parts[2] if len(parts) > 2 else ""
    date = parts[3] if len(parts) > 3 else ""

    numstat_raw = _run_git("show", commit, "--numstat", "--pretty=format:", cwd=workspace)
    diff_raw = _run_git("show", commit, "--unified=3", "--no-color", "--pretty=format:", cwd=workspace)
    files = _merge_hunks(_parse_numstat(numstat_raw), _parse_unified_diff(diff_raw))
    return _build_result(hash=commit_hash, message=message, author=author, date=date, files=files)


def workspace_diff(workspace: Path, scope: str = "worktree") -> dict[str, Any]:
    """Computes the ``CommitDiff`` for an agent's workspace. Synchronous —
    callers on the async request path must wrap this in
    ``asyncio.to_thread`` (subprocess.run blocks).

    Raises ``NoWorkspaceError`` if ``workspace`` doesn't exist on disk or
    isn't a git repository (or, for ``scope="last-commit"``, has no commits
    yet) — the router maps that to 404 ``{"reason": "no_workspace"}``.
    """
    if not workspace.is_dir():
        raise NoWorkspaceError(f"workspace path does not exist: {workspace}")

    # Cheap repo-ness check before running the real diff commands, so a
    # non-repo workspace root (common: agent.workspace_path is a parent dir
    # holding several project checkouts, not a repo itself) fails fast with
    # the same 404 contract as "no workspace" rather than a 500.
    _run_git("rev-parse", "--git-dir", cwd=workspace)

    if scope == "last-commit":
        return _commit_diff(workspace, "HEAD")
    return _worktree_diff(workspace)
