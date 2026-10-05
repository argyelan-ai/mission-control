"""Structured git diff over an agent's workspace — powers the diff panel in
Sessions Chat.

Two scopes:
  * ``"worktree"``    — uncommitted changes (staged + unstaged) against HEAD,
                          via ``git diff HEAD``, plus new files git does not
                          track yet (``git ls-files --others --exclude-standard``)
                          — a file the agent just created is the most common
                          "what just happened" and was invisible before.
  * ``"last-commit"``  — the most recent commit, via ``git show HEAD``.

The payload carries codes, never UI sentences (docs/PRINCIPLES.md §6.3):
``scope`` says which view it is, an uncommitted diff has an empty
``message``, ``committed_at`` is an ISO timestamp the UI formats in the
operator's locale. WHICH repository is shown is decided by
``choose_repo`` — see there for the order and why.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.services.fs_roots import sensitive_subpaths
from app.services.token_harvester import _host_home

logger = logging.getLogger("mc.workspace_diff")

_MAX_FILES = 200
_MAX_LINES_PER_FILE = 5000
#: An untracked file larger than this is listed without its content.
_MAX_UNTRACKED_BYTES = 1_000_000
#: git's well-known empty tree — the diff base for a repo without commits.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
#: Directory names never descended into while looking for repositories.
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".venv", "venv"})
#: Upper bound of directory entries ``repos_under`` looks at — keeps a poll
#: cheap on the bind-mounted workspace even for an agent with hundreds of
#: task folders.
_MAX_SCAN_ENTRIES = 2000

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
    prefix in practice). The backend container only mounts
    ``${HOME}/.mc:${HOME}/.mc`` (``docker-compose.yml``, backend service
    volumes) — not the full ``${HOME}`` tree — so a host path resolves
    unchanged in-container *because* every agent workspace lives under
    ``~/.mc/agents/{slug}/workspace`` or ``~/.mc/workspaces/{slug}``; a
    ``workspace_path`` pointing anywhere outside ``~/.mc`` is unreadable
    here and surfaces as a bare 404 ``no_workspace``. Only legacy/hand-edited
    rows that do carry a literal ``~`` need the ``token_harvester._host_home()``
    translation — mirrors ``token_harvester._expand_harvest_path``.
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
    *,
    scope: str,
    hash: str,
    message: str,
    author: str,
    date: str,
    files: list[dict[str, Any]],
    committed_at: str | None = None,
) -> dict[str, Any]:
    files = files[:_MAX_FILES]
    return {
        "scope": scope,
        "hash": hash,
        "message": message,
        "author": author,
        "date": date,
        "committed_at": committed_at,
        "stats": {
            "files": len(files),
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
        },
        "files": files,
    }


def _has_commits(workspace: Path) -> bool:
    try:
        _run_git("rev-parse", "--verify", "--quiet", "HEAD", cwd=workspace)
    except NoWorkspaceError:
        return False
    return True


def _untracked_file_entry(workspace: Path, name: str) -> dict[str, Any]:
    """A new, untracked file as a ``CommitDiffFile``: every line an addition.

    Read in Python, not via ``git add -N`` (that would write the index — a
    side effect in the agent's repo for a read-only panel). Symlinks,
    non-regular, binary and oversized files are listed WITHOUT content: a
    symlink could point outside the repository, and its target must never
    end up in the panel.
    """
    entry: dict[str, Any] = {"filename": name, "additions": 0, "deletions": 0, "hunks": []}
    path = workspace / name
    try:
        if path.is_symlink() or not path.is_file():
            return entry
        if path.stat().st_size > _MAX_UNTRACKED_BYTES:
            return entry
        data = path.read_bytes()
    except OSError:
        return entry
    if b"\0" in data[:8192]:
        return entry
    lines = data.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return entry
    shown = lines[:_MAX_LINES_PER_FILE]
    hunk_lines = [
        {"type": "add", "content": line, "old_no": None, "new_no": i + 1}
        for i, line in enumerate(shown)
    ]
    if len(lines) > len(shown):
        hunk_lines.append({"type": "ctx", "content": "… truncated", "old_no": None, "new_no": None})
    entry["additions"] = len(lines)
    entry["hunks"] = [{"header": f"@@ -0,0 +1,{len(lines)} @@", "lines": hunk_lines}]
    return entry


def _worktree_diff(workspace: Path) -> dict[str, Any]:
    base = "HEAD" if _has_commits(workspace) else _EMPTY_TREE
    numstat_raw = _run_git("diff", base, "--numstat", cwd=workspace)
    diff_raw = _run_git("diff", base, "--unified=3", "--no-color", cwd=workspace)
    files = _merge_hunks(_parse_numstat(numstat_raw), _parse_unified_diff(diff_raw))

    budget = _MAX_FILES - len(files)
    if budget > 0:
        untracked_raw = _run_git("ls-files", "--others", "--exclude-standard", "-z", cwd=workspace)
        names = [n for n in untracked_raw.split("\0") if n][:budget]
        files.extend(_untracked_file_entry(workspace, n) for n in names)

    # No ``message``: "uncommitted changes" is a UI label, and the UI owns
    # its wording in both languages (``scope`` tells it which view this is).
    return _build_result(scope="worktree", hash="", message="", author="", date="", files=files)


def _commit_diff(workspace: Path, commit: str) -> dict[str, Any]:
    if not _has_commits(workspace):
        # A repo the agent just created: honest "no commit yet", not the
        # same 404 as "there is no repository at all".
        return _build_result(scope="last-commit", hash="", message="", author="", date="", files=[])

    meta_raw = _run_git(
        "log", "-1", "--pretty=format:%h\x1f%s\x1f%an\x1f%ar\x1f%aI", commit, cwd=workspace
    )
    parts = meta_raw.split("\x1f", 4)
    commit_hash = parts[0] if len(parts) > 0 else commit[:7]
    message = parts[1] if len(parts) > 1 else ""
    author = parts[2] if len(parts) > 2 else ""
    date = parts[3] if len(parts) > 3 else ""
    committed_at = parts[4] if len(parts) > 4 and parts[4] else None

    numstat_raw = _run_git("show", commit, "--numstat", "--pretty=format:", cwd=workspace)
    diff_raw = _run_git("show", commit, "--unified=3", "--no-color", "--pretty=format:", cwd=workspace)
    files = _merge_hunks(_parse_numstat(numstat_raw), _parse_unified_diff(diff_raw))
    return _build_result(
        scope="last-commit",
        hash=commit_hash,
        message=message,
        author=author,
        date=date,
        files=files,
        committed_at=committed_at,
    )


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def git_dir(repo: Path) -> Path | None:
    """The real git directory of ``repo``: ``.git`` itself, or — for a
    ``git worktree`` checkout, where ``.git`` is a FILE — the directory its
    ``gitdir:`` line points to (relative paths resolve against the repo).
    ``None`` when it cannot be found from this process."""
    dotgit = repo / ".git"
    try:
        if dotgit.is_dir():
            return dotgit
        if not dotgit.is_file():
            return None
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:"):].strip())
    if not target.is_absolute():
        target = repo / target
    try:
        return target if target.is_dir() else None
    except OSError:
        return None


def git_activity(repo: Path) -> float:
    """When git last did something in ``repo`` (Unix seconds, 0 = unknown).

    The newest mtime of the per-checkout files git writes: ``HEAD``
    (checkout), ``index`` (add, commit, status refresh) and ``logs/HEAD``
    (every commit/checkout). Resolved through ``git_dir``, so a worktree
    checkout counts its OWN activity — the plain ``.git`` mtime used before
    never changes for a worktree (``.git`` is a file there), which hid every
    worktree behind older clones. The ``.git`` DIRECTORY mtime is left out
    on purpose: a commit in any worktree rewrites ``COMMIT_EDITMSG`` in the
    shared git dir and would credit the main clone with its worktrees'
    work. Only when the git dir can't be resolved (a ``gitdir:`` pointing
    to a path this process can't see) is the ``.git`` mtime the fallback.
    """
    gd = git_dir(repo)
    if gd is not None:
        candidates = [gd / "HEAD", gd / "index", gd / "logs" / "HEAD"]
    else:
        candidates = [repo / ".git"]
    newest = 0.0
    for p in candidates:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def current_branch(repo: Path) -> str | None:
    """The checked-out branch name, or the short commit id when HEAD is
    detached — read from ``HEAD`` directly, no subprocess."""
    gd = git_dir(repo)
    if gd is None:
        return None
    try:
        head = (gd / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        return ref.removeprefix("refs/heads/") or None
    return head[:7] or None


def repos_under(root: Path, depth: int = 2) -> list[Path]:
    """Every git repository at most ``depth`` levels below ``root`` (not
    ``root`` itself). A repository is not descended into; dot-folders and
    dependency folders are skipped. Two levels because the agent layout puts
    checkouts at ``<ws>/<task>/`` and ``<ws>/worktrees/<branch>/``."""
    found: list[Path] = []
    budget = [_MAX_SCAN_ENTRIES]

    def walk(directory: Path, level: int) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for child in entries:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if _is_git_repo(child):
                found.append(child)
            elif level < depth:
                walk(child, level + 1)

    walk(root, 1)
    return found


def enclosing_repo(path: Path, boundary: Path) -> Path | None:
    """``path`` itself or its nearest ancestor that is a git repository —
    never above ``boundary`` (the agent's workspace root, or the MC home for
    host agents), so a session folder can't climb into an unrelated repo."""
    try:
        path = path.resolve()
        boundary = boundary.resolve()
    except OSError:
        return None
    if path != boundary and boundary not in path.parents:
        return None
    current = path
    while True:
        if _is_git_repo(current):
            return current
        if current == boundary:
            return None
        current = current.parent


def host_path_for_agent_cwd(
    runtime: str | None, workspace_root: Path | None, cwd: str | None
) -> Path | None:
    """Maps a working directory a CLI recorded to the path THIS process can
    read, or ``None`` when it can't (or shouldn't) be read.

    * Docker (``cli-bridge``) agents see their workspace mounted at
      ``/workspace``; only paths under it map (onto ``workspace_root``).
      Anything else (``/home/agent``, ``/workspace-ref``) is not the agent's
      workspace and maps to ``None``. ``..`` cannot escape the root.
    * Host agents record host paths. They are accepted only inside the MC
      home (``~/.mc``) — the only tree the backend container mounts, and the
      only one it has any business reading for the operator's chat — and
      never inside its sensitive folders (``fs_roots.sensitive_subpaths``).

    The runtime split mirrors ``dispatch._container_workspace_path`` (the
    host -> container direction); it is a mount fact, not a harness detail.
    """
    if not cwd or not cwd.startswith("/"):
        return None
    if runtime == "cli-bridge":
        if workspace_root is None:
            return None
        norm = os.path.normpath(cwd)
        if norm == "/workspace":
            return workspace_root
        if not norm.startswith("/workspace/"):
            return None
        return workspace_root / norm[len("/workspace/"):]
    mc_home = _host_home() / ".mc"
    candidate = Path(os.path.normpath(cwd))
    if candidate != mc_home and mc_home not in candidate.parents:
        return None
    rel = candidate.relative_to(mc_home).parts
    if rel and rel[0] in sensitive_subpaths():
        # Secrets, agent tokens, logs, backups: the same trees the Files
        # API refuses — a session folder there lends nothing to the panel.
        return None
    return candidate


def cwd_boundary(runtime: str | None, workspace_root: Path | None) -> Path | None:
    """How far up from a session folder a repository may be looked for: the
    agent's own workspace for Docker agents, the MC home for host agents —
    the same trees ``host_path_for_agent_cwd`` lets through."""
    if runtime == "cli-bridge":
        return workspace_root
    return _host_home() / ".mc"


def display_path(runtime: str | None, workspace_root: Path | None, repo: Path) -> str:
    """How the panel names the repo's location: the path the agent itself
    uses (``/workspace/…`` for Docker agents), else ``~/…`` under the host
    home — never the operator's full home path in the UI."""
    if runtime == "cli-bridge" and workspace_root is not None:
        try:
            rel = repo.relative_to(workspace_root)
        except ValueError:
            rel = None
        if rel is not None:
            return "/workspace" if str(rel) == "." else f"/workspace/{rel.as_posix()}"
    try:
        return f"~/{repo.relative_to(_host_home()).as_posix()}"
    except ValueError:
        return str(repo)


@dataclass(frozen=True)
class RepoChoice:
    """The repository the panel shows, and WHY (a code the UI translates):

    * ``"task"``    — the workspace of the task the agent is running now;
    * ``"session"`` — the folder the chat session itself works in;
    * ``"recent"``  — the repository git touched last among the places the
      agent works (session folder, workspace root, last task).
    """

    path: Path
    kind: str


def choose_repo(
    *,
    running_task: Path | None,
    session_dir: Path | None,
    boundary: Path | None,
    fallbacks: Iterable[Path | None] = (),
) -> RepoChoice:
    """Which repository shows "what just happened" for this agent.

    1. A running task's workspace — explicit job context, wins.
    2. The chat session's own folder, when it is inside a repository.
    3. Otherwise the most recently git-active repository found in the
       session folder, the fallbacks (agent root, last task) and up to two
       levels below each.

    Before 04.10.2026 step 3 was "the most recently UPDATED task row" and
    won unconditionally — so a chat that just committed showed a months-old
    commit of a long-finished task (operator finding). Raises
    ``NoWorkspaceError`` when no repository is found anywhere.
    """
    if running_task is not None:
        try:
            return RepoChoice(find_repo_root(running_task), "task")
        except NoWorkspaceError:
            pass

    if session_dir is not None and boundary is not None:
        repo = enclosing_repo(session_dir, boundary)
        if repo is not None:
            return RepoChoice(repo, "session")

    pool: dict[Path, Path] = {}
    for root in (session_dir, *fallbacks):
        if root is None:
            continue
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        found = [root] if _is_git_repo(root) else repos_under(root)
        for repo in found:
            try:
                pool.setdefault(repo.resolve(), repo)
            except OSError:
                continue
    if not pool:
        raise NoWorkspaceError("no git repository in any candidate folder")
    best = max(pool.values(), key=git_activity)
    return RepoChoice(best, "recent")


def source_info(choice: RepoChoice, display: str) -> dict[str, Any]:
    """The ``source`` block of the diff payload — codes and machine values
    only (the UI translates ``kind``)."""
    return {
        "kind": choice.kind,
        "repo": choice.path.name,
        "branch": current_branch(choice.path),
        "path": display,
    }


def find_repo_root(workspace: Path) -> Path:
    """Finds the git repo the diff should run in, starting at ``workspace``.

    ``workspace`` itself wins when it is a repo. Otherwise the immediate
    children are scanned — the agent workspace layout puts the real checkout
    one level down (``<agent_ws>/<task-slug>/repo/`` for ad-hoc tasks whose
    agent cloned on its own, ``<agent_ws>/projects/<proj>/…`` for project
    tasks), so the parent dir is never a repo. With several child repos the
    most recently used one wins (``git_activity`` — resolves worktree
    checkouts to their real git dir). Raises ``NoWorkspaceError`` when
    nothing qualifies.

    Incident 2026-09-11: the panel showed "no workspace" for an agent with
    staged changes because only the non-repo root was ever inspected.
    """
    if not workspace.is_dir():
        raise NoWorkspaceError(f"workspace path does not exist: {workspace}")
    if _is_git_repo(workspace):
        return workspace
    try:
        children = [c for c in workspace.iterdir() if c.is_dir() and _is_git_repo(c)]
    except OSError as exc:
        raise NoWorkspaceError(f"workspace unreadable: {workspace}: {exc}") from exc
    if not children:
        raise NoWorkspaceError(f"no git repository in or directly under {workspace}")
    return max(children, key=git_activity)


def workspace_diff(workspace: Path, scope: str = "worktree") -> dict[str, Any]:
    """Computes the ``CommitDiff`` for an agent's workspace. Synchronous —
    callers on the async request path must wrap this in
    ``asyncio.to_thread`` (subprocess.run blocks).

    Raises ``NoWorkspaceError`` if ``workspace`` doesn't exist on disk or
    isn't a git repository — the router maps that to 404
    ``{"reason": "no_workspace"}``. A repository without commits is not an
    error: ``worktree`` diffs against the empty tree, ``last-commit``
    returns an empty result with ``hash == ""``.
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
