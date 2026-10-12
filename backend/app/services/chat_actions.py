"""chat_actions — the Changes panel's three write actions (chat package P3).

The operator's phone is the remote control of a chat: what the chat changed
can be put back, committed, or opened as a pull request — each behind one
explicit tap (operator decision 12.10.2026; ADR-089: operator-initiated,
the tap is the approval; never force, never a push to the default branch).

Where it runs: in the backend process, on the agent's workspace through the
same host path the read-only panel uses (``workspace_diff`` — the agent
folders are bind-mounted from the host, ``~/.mc``). No second channel into
the agent's container.

**Revert file** puts ONE file the chat changed back to its state before the
chat's first edit of it. The source is the transcript itself: every edit the
chat made, with its raw before/after (``chat_diff.capture_texts``). Walking
the edits backwards from the file on disk both proves the file still is what
the chat left (a whole-file edit must match exactly, a snippet edit must
still be there) and yields the content before the first edit. When the file
changed since — by hand, by a bash command, by another tool — it is refused
with ``changed_externally``, never overwritten (the ZCode rule). A file the
chat created is deleted. Inside a git repository, when the reconstructed
original is exactly the last committed version, git restores it
(``git checkout HEAD -- <file>``, index included); when MC cannot know the
original (a Claude ``Write`` over an unknown file) but the file is tracked
and verified unchanged, the last committed version is restored and the
answer says so (``method: "git"``).

**Commit** stages everything uncommitted (MC's own helper files excluded) and
commits it with the operator's (suggested, editable) message. Hooks run
(never ``--no-verify``). Refused on a detached HEAD and on the default branch
of a repository with a remote — that work belongs on a branch.

**Open PR** pushes the checked-out branch normally (``git push -u origin
<branch>``, never ``--force``) and opens the pull request through the same
``GitService`` path MC's task flow already uses (``push_branch`` +
``create_pr``). Refused for the default branch, ``main``/``master``, a
detached HEAD, no ``origin`` or a non-GitHub remote. An open PR for the
branch is returned instead of a second one (idempotent).

Every refusal is a ``Refused`` with a machine ``reason`` the UI translates
(docs/PRINCIPLES.md §6.3: codes, never sentences, from the backend).
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.services.chat_changes import _normalise
from app.services.workspace_diff import (
    MC_HELPER_FILES,
    current_branch,
    enclosing_repo,
    host_path_for_agent_cwd,
    is_mc_helper_file,
)

#: A file larger than this is not restored by MC (the transcript would not
#: carry it whole either; the bridge caps texts at 60k).
MAX_RESTORE_BYTES = 2_000_000
#: Subject line of a suggested commit message.
_MAX_SUBJECT = 72
#: Files named in a suggested commit body.
_MAX_BODY_FILES = 20
#: Branch names that are never pushed from the panel, whatever the remote says.
PROTECTED_BRANCHES = frozenset({"main", "master"})
#: Fallback identity when the checkout has none — the same one GitService
#: configures for MC's own commits.
_MC_IDENTITY = ("Mission Control", "mc@mc.local")


class Refused(Exception):
    """An action MC will not do, with the reason code the UI translates.
    ``status`` is the HTTP status the router answers with."""

    def __init__(self, reason: str, status: int = 409, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.detail = detail

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reason": self.reason}
        if self.detail:
            out["detail"] = self.detail[:300]
        return out


def sha256_text(text: str | None) -> str:
    """Fingerprint of a file's content; ``"absent"`` for no file."""
    if text is None:
        return "absent"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# Revert — which edits, and what was there before them
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Edit:
    """One edit of one file, as the transcript recorded it."""

    kind: str  # "edit" | "create" | "write"
    scope: str  # "file" (whole-file texts) | "snippet" (Claude old/new_string)
    old: str | None
    new: str | None
    complete: bool  # the harness handed the texts over uncut
    replace_all: bool = False


@dataclass
class FileEdits:
    key: str  # the agent-side absolute (or as-written) path
    edits: list[Edit] = field(default_factory=list)
    tool_calls: int = 0


def collect_edits(events: Iterable[dict[str, Any]], cwd: str | None, path: str) -> FileEdits:
    """Every successful edit of ``path`` in this chat, oldest first.

    ``events`` must come from ``read_history`` inside
    ``chat_diff.capture_texts()`` — a diff without ``_texts`` cannot be
    restored and makes the whole file non-restorable (``complete=False``).
    ``path`` is the display path of the Changes list (relative to the
    session folder) or an absolute path; both normalise like
    ``chat_changes``. No edits -> ``Refused("not_changed_by_chat", 404)``."""
    target = _normalise(path.strip(), cwd)
    if is_mc_helper_file(target):
        raise Refused("not_changed_by_chat", 404)
    found = FileEdits(key=target)
    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") != "tool" or ev.get("status") == "error":
            continue
        diffs = ev.get("diffs")
        if not isinstance(diffs, list):
            continue
        touched = False
        for diff in diffs:
            if not isinstance(diff, dict):
                continue
            raw = diff.get("path")
            if not isinstance(raw, str) or not raw.strip():
                continue
            if _normalise(raw.strip(), cwd) != target:
                continue
            texts = diff.get("_texts") if isinstance(diff.get("_texts"), dict) else {}
            found.edits.append(Edit(
                kind=str(diff.get("kind") or "edit"),
                scope=str(texts.get("scope") or "file"),
                old=texts.get("old") if isinstance(texts.get("old"), str) else None,
                new=texts.get("new") if isinstance(texts.get("new"), str) else None,
                complete=bool(texts) and bool(texts.get("complete")),
                replace_all=bool(texts.get("replaceAll")),
            ))
            touched = True
        if touched:
            found.tool_calls += 1
    if not found.edits:
        raise Refused("not_changed_by_chat", 404)
    return found


@dataclass(frozen=True)
class Original:
    """What the file looked like before the chat's first edit.

    ``known`` False = MC verified the file is still what the chat left, but
    cannot say what was there before (``reason`` says why) — only git can
    then restore it."""

    known: bool
    content: str | None = None  # None with known=True: the file did not exist
    reason: str | None = None


def reconstruct_original(edits: list[Edit], current: str | None) -> Original:
    """Walks the edits backwards from the file on disk.

    * A whole-file edit (ACP ``oldText``/``newText``, a Claude ``Write``):
      the content at that point must equal its ``new`` EXACTLY — otherwise
      the file changed after the chat (``changed_externally``). Then its
      ``old`` is the content before it (``None`` = the file did not exist).
      A ``write`` has no known ``old``: verified, but the original is
      unknown (``unknown_original``).
    * A snippet edit (Claude ``Edit``/``MultiEdit``): its ``new_string``
      must still be there exactly once and is swapped back for its
      ``old_string``. Gone -> ``changed_externally``; ambiguous (twice,
      empty, ``replace_all``) -> ``cannot_restore``.

    Raises ``Refused`` when the file is not what the chat left behind;
    returns ``Original(known=False)`` when it is, but its start is unknown."""
    content = current
    verified = False
    for e in reversed(edits):
        if e.scope == "file":
            if not e.complete or e.new is None:
                if verified:
                    return Original(False, reason="too_large")
                raise Refused("too_large")
            if content != e.new:
                raise Refused("changed_externally")
            verified = True
            if e.kind == "write":
                return Original(False, reason="unknown_original")
            content = e.old  # None: created by this edit
            continue
        # snippet
        if not e.complete or e.old is None or e.new is None:
            raise Refused("cannot_restore")
        if content is None or (e.new and e.new not in content):
            raise Refused("changed_externally")
        if e.replace_all or not e.new or content.count(e.new) != 1:
            if verified:
                return Original(False, reason="cannot_restore")
            raise Refused("cannot_restore")
        content = content.replace(e.new, e.old, 1)
    return Original(True, content)


# ══════════════════════════════════════════════════════════════════════════
# Revert — where the file is, and putting it back
# ══════════════════════════════════════════════════════════════════════════


def resolve_target(
    key: str,
    *,
    runtime: str | None,
    workspace_root: Path | None,
    allowed_roots: list[Path],
) -> tuple[Path, Path]:
    """The host path of the agent-side path ``key`` and the allowed root it
    lies in — or ``Refused("path_outside", 400)``.

    Only folders MC knows for this chat qualify (``allowed_roots``: the
    session folder, a running task's repository). Checked on the RESOLVED
    parent, so neither ``..`` nor a symlinked folder leads out; a symlink as
    the file itself and anything inside ``.git`` are refused as well."""
    if not key.startswith("/"):
        raise Refused("path_outside", 400)
    if ".git" in posixpath.normpath(key).split("/"):
        raise Refused("path_outside", 400)
    host = host_path_for_agent_cwd(runtime, workspace_root, key)
    if host is None:
        raise Refused("path_outside", 400)
    try:
        parent_real = Path(os.path.realpath(host.parent))
    except OSError:
        raise Refused("path_outside", 400) from None
    for root in allowed_roots:
        try:
            root_real = Path(os.path.realpath(root))
        except OSError:
            continue
        if parent_real == root_real or root_real in parent_real.parents:
            target = parent_real / host.name
            if target.is_symlink():
                raise Refused("path_outside", 400)
            return target, root_real
    raise Refused("path_outside", 400)


def read_current(path: Path) -> str | None:
    """The file's text as it is now, ``None`` when it does not exist."""
    try:
        if not path.exists():
            return None
        if not path.is_file():
            raise Refused("cannot_restore")
        if path.stat().st_size > MAX_RESTORE_BYTES:
            raise Refused("too_large")
        data = path.read_bytes()
    except OSError as exc:
        raise Refused("cannot_restore", detail=str(exc)) from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("cannot_restore") from None


def _write_atomic(path: Path, text: str) -> None:
    """Writes via a temp file in the same folder + rename, keeping the
    file's mode — a reader never sees half a file."""
    mode = None
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".mc-revert-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _git(*args: str, cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    """git with an argv list, never a shell; no prompts. Callers check
    ``returncode`` themselves."""
    return subprocess.run(
        ["git", "--no-pager", "-c", "safe.directory=*", "-c", "core.quotepath=false", *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _head_text(repo: Path, rel: str) -> str | None:
    """The file's content in HEAD, ``None`` when HEAD has no such file (or
    the repository no commits)."""
    res = _git("show", f"HEAD:{rel}", cwd=repo)
    return res.stdout if res.returncode == 0 else None


def revert_file(target: Path, root: Path, edits: list[Edit]) -> dict[str, Any]:
    """Puts ``target`` back to its state before the chat's edits.

    Returns ``{"status": "reverted" | "already", "method": "chat" | "git",
    "deleted": bool, "resultSha": str}``; raises ``Refused``."""
    current = read_current(target)
    original = reconstruct_original(edits, current)

    repo = enclosing_repo(target.parent, root)
    rel = target.relative_to(repo).as_posix() if repo is not None else None

    if not original.known:
        # MC verified the file is what the chat left, but not what was
        # there before. Only git knows — when the file is tracked.
        head = _head_text(repo, rel) if repo is not None and rel is not None else None
        if head is None:
            raise Refused(original.reason or "unknown_original")
        res = _git("checkout", "HEAD", "--", rel, cwd=repo)
        if res.returncode != 0:
            raise Refused("git_failed", detail=_first_line(res.stderr))
        return {"status": "reverted", "method": "git", "deleted": False, "resultSha": sha256_text(head)}

    if original.content == current:
        return {"status": "already", "method": "chat", "deleted": current is None,
                "resultSha": sha256_text(current)}

    if original.content is None:
        # Created by this chat: remove it — and its staged "add", if any.
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise Refused("cannot_restore", detail=str(exc)) from None
        if repo is not None and rel is not None and _head_text(repo, rel) is None:
            _git("rm", "-q", "--cached", "--ignore-unmatch", "--", rel, cwd=repo)
        return {"status": "reverted", "method": "chat", "deleted": True, "resultSha": sha256_text(None)}

    if repo is not None and rel is not None and _head_text(repo, rel) == original.content:
        res = _git("checkout", "HEAD", "--", rel, cwd=repo)
        if res.returncode == 0:
            return {"status": "reverted", "method": "git", "deleted": False,
                    "resultSha": sha256_text(original.content)}
        # git refused (lock, odd state): the chat's own record still knows
        # the content — fall through and write it.

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, original.content)
    except OSError as exc:
        raise Refused("cannot_restore", detail=str(exc)) from None
    return {"status": "reverted", "method": "chat", "deleted": False,
            "resultSha": sha256_text(original.content)}


# ══════════════════════════════════════════════════════════════════════════
# Commit
# ══════════════════════════════════════════════════════════════════════════


def has_remote(repo: Path) -> bool:
    return _git("remote", "get-url", "origin", cwd=repo).returncode == 0


def remote_url(repo: Path) -> str | None:
    res = _git("remote", "get-url", "origin", cwd=repo)
    return res.stdout.strip() if res.returncode == 0 else None


def default_branch(repo: Path) -> str:
    """The remote's default branch as git knows it locally (``origin/HEAD``),
    else ``main``/``master`` when such a remote branch exists, else
    ``main``. Local refs only — never fetches."""
    res = _git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=repo)
    if res.returncode == 0 and res.stdout.strip().startswith("origin/"):
        return res.stdout.strip()[len("origin/"):]
    for name in ("main", "master"):
        if _git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}", cwd=repo).returncode == 0:
            return name
    return "main"


def _helper_excludes() -> list[str]:
    return [f":(exclude,glob)**/{name}" for name in sorted(MC_HELPER_FILES)]


def pending_files(repo: Path) -> list[dict[str, Any]]:
    """What a commit would take: every uncommitted file (staged, unstaged,
    untracked; MC helper files left out), with +/− when git can count
    them. ``status`` is ``added``/``modified``/``deleted``/``renamed``."""
    res = _git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=repo)
    if res.returncode != 0:
        raise Refused("git_failed", detail=_first_line(res.stderr))
    entries: list[dict[str, Any]] = []
    parts = res.stdout.split("\0")
    i = 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        code, name = item[:2], item[3:]
        if code[0] in "RC":
            i += 1  # the rename's source path follows
        if is_mc_helper_file(name):
            continue
        if code == "??" or "A" in code:
            status = "added"
        elif "D" in code:
            status = "deleted"
        elif code[0] in "RC":
            status = "renamed"
        else:
            status = "modified"
        entries.append({"path": name, "status": status, "additions": 0, "deletions": 0})
    if not entries:
        return entries
    has_head = _git("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo).returncode == 0
    if has_head:
        stats = _git("diff", "HEAD", "--numstat", cwd=repo)
        counts: dict[str, tuple[int, int]] = {}
        for line in stats.stdout.splitlines():
            bits = line.split("\t", 2)
            if len(bits) == 3:
                a, d, n = bits
                counts[n] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
        for e in entries:
            if e["path"] in counts:
                e["additions"], e["deletions"] = counts[e["path"]]
    for e in entries:
        if e["status"] == "added" and e["additions"] == 0:
            try:
                p = repo / e["path"]
                if p.is_file() and not p.is_symlink() and p.stat().st_size <= MAX_RESTORE_BYTES:
                    e["additions"] = len(p.read_bytes().decode("utf-8", errors="replace").splitlines())
            except OSError:
                pass
    return entries


def _commit_type(paths: list[str], statuses: list[str]) -> str:
    def is_doc(p: str) -> bool:
        return p.startswith("docs/") or p.endswith((".md", ".rst", ".txt"))

    def is_test(p: str) -> bool:
        base = p.rsplit("/", 1)[-1]
        return (
            p.startswith(("tests/", "test/")) or "/tests/" in p or "/__tests__/" in p
            or base.startswith("test_") or ".test." in base or ".spec." in base
        )

    if all(is_doc(p) for p in paths):
        return "docs"
    if all(is_test(p) for p in paths):
        return "test"
    if all(s == "added" for s in statuses):
        return "feat"
    return "chore"


def suggest_commit_message(files: list[dict[str, Any]]) -> str:
    """A conventional-commit message from the files about to be committed:
    the subject names the type and the files, the body lists each with its
    +/−. Deterministic and local — no model call; the operator edits it."""
    if not files:
        return ""
    paths = [f["path"] for f in files]
    statuses = [f["status"] for f in files]
    kind = _commit_type(paths, statuses)
    verb = {"feat": "add", "docs": "update", "test": "update", "chore": "update"}[kind]
    if all(s == "deleted" for s in statuses):
        verb = "remove"
    names = [p.rsplit("/", 1)[-1] for p in paths]
    if len(names) == 1:
        what = names[0]
    elif len(names) == 2:
        what = f"{names[0]} and {names[1]}"
    else:
        what = f"{names[0]} and {len(names) - 1} more files"
    subject = f"{kind}: {verb} {what}"
    if len(subject) > _MAX_SUBJECT:
        subject = subject[: _MAX_SUBJECT - 1].rstrip() + "…"
    body = []
    for f in files[:_MAX_BODY_FILES]:
        counts = []
        if f.get("additions"):
            counts.append(f"+{f['additions']}")
        if f.get("deletions"):
            counts.append(f"-{f['deletions']}")
        suffix = f" ({' '.join(counts)})" if counts else ""
        body.append(f"- {f['path']}{suffix}")
    if len(files) > _MAX_BODY_FILES:
        body.append(f"- … {len(files) - _MAX_BODY_FILES} more")
    return subject + "\n\n" + "\n".join(body) + "\n"


def commit_guard(repo: Path) -> str:
    """The branch a commit lands on, or ``Refused``: a detached HEAD has
    none, and the default branch of a repository with a remote is not
    where a chat's work is committed from the panel."""
    branch = current_branch(repo)
    if not branch or _git("symbolic-ref", "--quiet", "HEAD", cwd=repo).returncode != 0:
        raise Refused("detached_head")
    if has_remote(repo) and (branch == default_branch(repo) or branch in PROTECTED_BRANCHES):
        raise Refused("default_branch")
    return branch


def commit_all(repo: Path, message: str) -> dict[str, Any]:
    """Stages every uncommitted change (MC helper files excluded) and
    commits it. Hooks run. Nothing to commit -> ``status:
    "nothing_to_commit"`` with the current HEAD (a repeated tap is
    harmless)."""
    if not message.strip():
        raise Refused("message_required", 400)
    branch = commit_guard(repo)
    add = _git("add", "-A", "--", ".", *_helper_excludes(), cwd=repo)
    if add.returncode != 0:
        raise Refused("git_failed", detail=_first_line(add.stderr))
    staged = _git("diff", "--cached", "--quiet", cwd=repo)
    if staged.returncode == 0:
        head = _git("rev-parse", "--short", "HEAD", cwd=repo)
        return {"status": "nothing_to_commit", "branch": branch,
                "sha": head.stdout.strip() if head.returncode == 0 else None}
    identity: list[str] = []
    if not _git("config", "user.email", cwd=repo).stdout.strip():
        identity = ["-c", f"user.name={_MC_IDENTITY[0]}", "-c", f"user.email={_MC_IDENTITY[1]}"]
    res = _git(*identity, "commit", "-q", "-F", "-", cwd=repo, stdin=message.strip() + "\n")
    if res.returncode != 0:
        raise Refused("git_failed", detail=_first_line(res.stderr) or _first_line(res.stdout))
    sha = _git("rev-parse", "--short", "HEAD", cwd=repo).stdout.strip()
    return {"status": "committed", "branch": branch, "sha": sha,
            "subject": _first_line(message)}


# ══════════════════════════════════════════════════════════════════════════
# Pull request
# ══════════════════════════════════════════════════════════════════════════


def pr_guard(repo: Path) -> dict[str, Any]:
    """What a PR would be — ``{branch, base, title, ahead, uncommitted}`` —
    or ``Refused``. Synchronous, local refs only."""
    branch = current_branch(repo)
    if not branch or _git("symbolic-ref", "--quiet", "HEAD", cwd=repo).returncode != 0:
        raise Refused("detached_head")
    url = remote_url(repo)
    if not url:
        raise Refused("no_remote")
    if "github.com" not in url:
        raise Refused("not_github")
    base = default_branch(repo)
    if branch == base or branch in PROTECTED_BRANCHES:
        raise Refused("default_branch")
    head = _git("log", "-1", "--format=%s", cwd=repo)
    if head.returncode != 0:
        raise Refused("no_commits")
    ahead = None
    count = _git("rev-list", "--count", f"refs/remotes/origin/{base}..HEAD", cwd=repo)
    if count.returncode == 0 and count.stdout.strip().isdigit():
        ahead = int(count.stdout.strip())
        if ahead == 0:
            raise Refused("no_commits")
    return {
        "branch": branch,
        "base": base,
        "title": head.stdout.strip(),
        "ahead": ahead,
        "uncommitted": len(pending_files(repo)),
    }
