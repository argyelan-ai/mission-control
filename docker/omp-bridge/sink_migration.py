#!/usr/bin/env python3
"""
sink_migration.py — move MC's old display copies out of omp's sight (S1-R3).

WHY: MC writes a copy of every ACP session for the chat view
(``acp_chat_events.ChatEventSink``). Until S1-R3 that copy sat NEXT TO omp's
own session file, with the same session id in its header. omp 18.10.1 takes
both for sessions and loads the younger one — often MC's copy: live
20.10.2026 the current chat failed to load and started over after a daemon
restart. New copies now go to ``<session folder>/.mc/``; this script moves
the ones written before.

WHAT IT MOVES: in every folder directly under the sessions root (no dot
folders, no symlinks), every ``*.jsonl`` directly in the folder whose session
header names the bridge (``"bridge": "acp"`` — omp never writes that key) goes
to ``<folder>/.mc/<same name>``; ``<folder>/media/*`` goes to
``<folder>/.mc/media/`` — only in a folder that holds an MC copy (moved now or
before), so a wrong ``--root`` moves no unrelated pictures. Same file system,
so each move is one atomic rename that keeps the modification time (the
backend ranks sessions by it).

IT NEVER LEAVES THE ROOT: the session folders are the agent's own mount, so
the agent can plant a symlink there, and the operator runs this on the HOST
with the host's rights. Every folder between the root and a source or a
target must be a real folder (no symlink, no file) — checked when planning
and again at each rename; a ``.mc`` or ``.mc/media`` that is not a real
folder skips that folder with reason ``unsafe_target``. Only regular files
move. The list is written through a fresh temp file (``O_EXCL``), never
through a name someone planted. The agent should be stopped while the host
runs (deploy window); the checks are the guard if it was not.

WHAT IT NEVER DOES: delete or overwrite (a target that exists is skipped, the
reason goes into the list); touch any subfolder (``previews/``, omp's
artifact folders — also those with MC's name pattern, live
``2026-09-16T01-06-13_<id>/``), any dot file (omp's ``.<name>.jsonl.lock.os``),
``acp-chat-state.json``, ``acp-chat.json`` or omp's own files; create a
``.bak`` name (omp renames ``*.jsonl.<x>.bak`` back into sessions).

THE LIST: ``<root>/.mc-sink-migration.json`` — never ``.jsonl``, the
backend's ``*.jsonl`` search would read it as a session. A run that moved
something is appended by rewriting the file atomically; a run from the
command line also when it only skipped something (a refused move stays on
record). A run with nothing to do is not recorded, nor a daemon run that
only skipped (it would repeat at every start). Paths are relative to the
root, because the host and the container see the same tree under different
absolute paths. A run is written BEFORE its moves (status ``started``) and
again after (``done``): a crash in between still leaves a list ``rollback``
can follow. A list that cannot be read stops ``--apply`` and a plain
``--rollback``; ``--rollback --all`` needs no list (it finds MC's copies by
their header) and records its run in a fresh ``.mc-sink-migration.<time>.json``
beside the damaged one, which stays untouched.

THE LOCK: ``<root>/.mc-sink-migration.lock`` (``O_EXCL``) with pid, time and
host. A lock older than ``LOCK_STALE_S`` or of a dead process on the same
host is taken over; a fresh one stops the run (status ``locked``) — the
chat daemon then says so in the chat. A lock time more than
``LOCK_CLOCK_SLACK_S`` in the future is no time at all: the lock is judged by
its file time instead (a lock file from the future is stale). A lock that
cannot be created or removed is status ``error`` (permissions), not
``locked``. The chat daemon plans read-only first and takes the lock only
when something is to move — no write at the root at every start.

WHO RUNS IT: the operator, on the host, in the deploy window with the agent
stopped — ``python3 sink_migration.py --root DIR --dry-run`` (counts only),
then ``--apply``; ``--rollback [--all]`` is the way back. As a safety net the
chat daemon also runs ``migrate`` in ``ChatSession.start()`` before it reads
its pointer, and ``remap`` sends an old pointer to the moved file.

Standard library only; runs on Python 3.9 (the host) and 3.11 (the image).
omp's header rule, MC's header mark and ``MC_SINK_DIRNAME`` come from
``acp_chat_events`` (the same folder, in the image and in a checkout — the
Dockerfile copies both): one rule, not two (tests/omp_listing_rule.py is
the independent test twin).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acp_chat_events import (  # noqa: E402 — the one header rule
    MC_SINK_DIRNAME,
    is_mc_header,
    open_regular,
    session_header,
)

#: The list of every move (at the sessions root). ``.json`` on purpose.
LIST_NAME = ".mc-sink-migration.json"
#: The lock (at the sessions root).
LOCK_NAME = ".mc-sink-migration.lock"
#: A lock older than this is taken over (seconds). A real run takes well
#: under a second; ten minutes only guards against a run that died.
LOCK_STALE_S = 600
#: Host and container clocks may differ a little: a lock dated up to this far
#: ahead is a live run; further ahead the time is garbage (seconds).
LOCK_CLOCK_SLACK_S = 60
LIST_VERSION = 1
MEDIA_DIRNAME = "media"
#: Files in a session folder that are MC's but never move.
NEVER_TOUCH = frozenset({"acp-chat-state.json", "acp-chat.json"})
#: MC's line ids start with this (acp_chat_events._ID_PREFIX); omp's are hex.
_MC_ID_PREFIX = "acp"
#: Line types only omp writes — a sign that omp wrote into MC's copy.
_OMP_ONLY_TYPES = frozenset({"model_change", "thinking_level_change", "session_exit"})

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOCKED = 3


class ListError(Exception):
    """The list exists but cannot be read. Nothing moves then: the list is
    the only record of what went where, and a run must never start a new
    one over it."""


@dataclass
class Report:
    """What a run did (or, for a dry run, would do)."""

    action: str
    root: str
    dry_run: bool = False
    #: ok · locked · missing (no such root) · error
    status: str = "ok"
    detail: str = ""
    #: {from, to, sessionId, task, mixed} — paths relative to the root.
    moved: list = field(default_factory=list)
    #: {from, to}
    media: list = field(default_factory=list)
    #: {path, reason}
    skipped: list = field(default_factory=list)
    #: omp's own session files seen (and left alone).
    omp_files: int = 0

    def counts(self) -> dict:
        return {
            "moved": len(self.moved),
            "mixed": sum(1 for e in self.moved if e.get("mixed")),
            "task": sum(1 for e in self.moved if e.get("task")),
            "media": len(self.media),
            "skipped": len(self.skipped),
        }

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "root": self.root,
            "dryRun": self.dry_run,
            "status": self.status,
            "detail": self.detail,
            "counts": self.counts(),
            "ompFiles": self.omp_files,
            "moved": self.moved,
            "media": self.media,
            "skipped": self.skipped,
        }

    def summary(self) -> str:
        counts = self.counts()
        title = "rollback" if self.action == "rollback" else "migration"
        mode = " (dry run — nothing changed)" if self.dry_run else ""
        lines = [
            f"sink {title}{mode} in {self.root}",
            f"status: {self.status}" + (f" — {self.detail}" if self.detail else ""),
        ]
        unsafe = [e["path"] for e in self.skipped if e.get("reason") == "unsafe_target"]
        if unsafe:
            # Not the expected numbers: someone put a link (or a file) where
            # a folder belongs. Nothing went there; look at it by hand.
            lines.append(f"WARNING: {len(unsafe)} place(s) are a symlink or a file where a "
                         "real folder belongs — nothing moved through them; check by hand")
        lines += [
            f"moves: {counts['moved']} (mixed: {counts['mixed']}, task copies: {counts['task']})",
            f"media: {counts['media']}",
            f"skipped: {counts['skipped']}",
        ]
        if self.action == "migrate":
            lines.append(f"omp files left in place: {self.omp_files}")
        for entry in self.moved:
            mixed = entry.get("mixed")
            if mixed:
                lines.append(
                    f"  mixed {entry['from']}: {mixed['foreignLines']} lines omp wrote "
                    f"({', '.join(mixed['types'])})"
                )
        for entry in self.skipped:
            lines.append(f"  skipped {entry['path']}: {entry['reason']}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# reading the tree
# ---------------------------------------------------------------------------


def mixed_lines(path: Path) -> Optional[dict]:
    """Lines omp wrote into MC's copy (a "mixed file", live 16.09.2026):
    ``{"foreignLines": n, "types": [...]}``, or None when there are none.
    A line is omp's when its ``id`` is not MC's (``acp…``) or its type is
    one only omp writes."""
    count = 0
    types: set = set()
    fh = open_regular(path)
    if fh is None:
        return None
    try:
        with fh:
            for index, raw in enumerate(fh):
                if index == 0:
                    continue  # the header carries the session id, not a line id
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(line, dict) or line.get("type") in ("session", "title"):
                    continue
                line_id = line.get("id")
                foreign_id = isinstance(line_id, str) and not line_id.startswith(_MC_ID_PREFIX)
                if foreign_id or line.get("type") in _OMP_ONLY_TYPES:
                    count += 1
                    if isinstance(line.get("type"), str):
                        types.add(line["type"])
    except (OSError, ValueError):
        return None
    return {"foreignLines": count, "types": sorted(types)} if count else None


def _session_folders(root: Path) -> list:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [p for p in entries
            if not p.name.startswith(".") and p.is_dir() and not p.is_symlink()]


def _plain_files(folder: Path) -> list:
    """Regular files directly in ``folder``: no subfolders, no symlinks, no
    dot names."""
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    return [p for p in entries
            if not p.name.startswith(".") and p.is_file() and not p.is_symlink()]


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _real_dir(path: Path) -> bool:
    """A folder that is no symlink (``lstat``)."""
    try:
        return stat.S_ISDIR(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def _unsafe(root: Path, path: Path) -> Optional[str]:
    """Why ``path`` is no safe place below ``root`` — ``unsafe_target`` when
    a folder between the root and it is a symlink or a file (a followed
    link would carry a file out of the tree, or a host file in) — or None.
    Folders that do not exist yet are fine: the move creates them below a
    real folder."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return "unsafe_target"
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            mode = os.lstat(str(current)).st_mode
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"error: {exc.strerror or exc}"
        if not stat.S_ISDIR(mode):
            return "unsafe_target"
    return None


def _holds_mc_copy(copies: Path) -> bool:
    """``<folder>/.mc`` is a real folder with at least one MC copy in it."""
    if not _real_dir(copies):
        return False
    return any(p.name.endswith(".jsonl") and is_mc_header(session_header(p))
               for p in _plain_files(copies))


def _safe(root: Path, rel: Any) -> Optional[Path]:
    """``root/rel`` for a relative path that stays inside the root — a list
    entry must never send a file elsewhere."""
    if not isinstance(rel, str) or not rel:
        return None
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return root / candidate


def _plan(root: Path) -> tuple:
    """(moves, media, skipped, omp_files) — read only."""
    moves: list = []
    media: list = []
    skipped: list = []
    omp_files = 0
    for folder in _session_folders(root):
        copies = folder / MC_SINK_DIRNAME
        if os.path.lexists(str(copies)) and not _real_dir(copies):
            # A symlink (or a file) where MC's folder belongs: following it
            # would move copies anywhere the link points. The whole folder
            # stays as it is.
            skipped.append({"path": _rel(root, copies), "reason": "unsafe_target"})
            continue
        folder_moves: list = []
        for path in _plain_files(folder):
            if not path.name.endswith(".jsonl") or path.name in NEVER_TOUCH:
                continue
            header = session_header(path)
            if header is None:
                continue  # no session at all: neither omp's nor ours
            if not is_mc_header(header):
                omp_files += 1
                continue
            target = copies / path.name
            if os.path.lexists(str(target)):
                skipped.append({"path": _rel(root, path), "reason": "target_exists"})
                continue
            folder_moves.append({
                "from": _rel(root, path),
                "to": _rel(root, target),
                "sessionId": header["id"],
                "task": bool(header.get("taskId")),
                "mixed": mixed_lines(path),
            })
        moves.extend(folder_moves)
        pictures = folder / MEDIA_DIRNAME
        if not _real_dir(pictures):
            continue
        if not folder_moves and not _holds_mc_copy(copies):
            continue  # pictures follow MC's copies — nothing else
        picture_target = copies / MEDIA_DIRNAME
        if os.path.lexists(str(picture_target)) and not _real_dir(picture_target):
            skipped.append({"path": _rel(root, picture_target), "reason": "unsafe_target"})
            continue
        for path in _plain_files(pictures):
            target = picture_target / path.name
            if os.path.lexists(str(target)):
                skipped.append({"path": _rel(root, path), "reason": "target_exists"})
                continue
            media.append({"from": _rel(root, path), "to": _rel(root, target)})
    return moves, media, skipped, omp_files


# ---------------------------------------------------------------------------
# the lock
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    except OSError:
        return False
    return True


def _read_json(path: Path) -> Any:
    """The JSON in a regular file, or None (missing, no regular file — a
    FIFO never blocks —, unreadable, no JSON)."""
    fh = open_regular(path)
    if fh is None:
        return None
    try:
        with fh:
            return json.loads(fh.read())
    except (OSError, ValueError):
        return None


def _lock_state(path: Path, now: float) -> tuple:
    """(state, why) for an EXISTING lock file — state ``free``, ``held`` or
    ``error``. Judged by its age first; a lock of the same host is also
    free when its process is gone. A lock of another host (host vs.
    container) can only be judged by age. The age comes from the time in
    the lock; a time too far in the future (``LOCK_CLOCK_SLACK_S``) is
    garbage, and the file's own time counts instead."""
    try:
        mtime = os.lstat(str(path)).st_mtime
    except FileNotFoundError:
        return "free", "lock vanished"
    except OSError as exc:
        return "error", f"lock unreadable: {exc.strerror or exc}"
    info = _read_json(path)
    at = info.get("at") if isinstance(info, dict) else None
    future_at = ""
    stamp = mtime
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        if at <= now + LOCK_CLOCK_SLACK_S:
            stamp = at
        else:
            future_at = f", its time {int(at - now)} s in the future, judged by file time"
    age = now - stamp
    if not isinstance(info, dict):
        what = "lock without content"
        pid = None
    else:
        pid = info.get("pid")
        what = f"lock of pid {pid}"
    if age < -LOCK_CLOCK_SLACK_S:
        return "free", f"stale {what} (dated {int(-age)} s in the future)"
    if age > LOCK_STALE_S:
        return "free", f"stale {what} (age {int(age)} s{future_at})"
    if isinstance(info, dict) and info.get("host") == socket.gethostname() \
            and isinstance(pid, int) and not _pid_alive(pid):
        return "free", f"stale {what} (process gone)"
    return "held", f"locked ({what}, age {int(age)} s{future_at})"


def _acquire(root: Path, now: float) -> tuple:
    """(status, note) — ``acquired``, ``locked`` (another run holds it) or
    ``error`` (the lock cannot be created or cleared: permissions). Takes
    over a stale lock once."""
    path = root / LOCK_NAME
    note = ""
    for attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            state, why = _lock_state(path, now)
            if state == "error":
                return "error", why
            if state == "held" or attempt:
                return "locked", why
            try:
                os.unlink(str(path))  # a lock holds no data; a stale one is ours to clear
            except FileNotFoundError:
                pass
            except OSError as exc:
                return "error", f"stale lock not removable: {exc.strerror or exc}"
            note = f"took over {why}"
            continue
        except OSError as exc:
            return "error", f"lock not creatable: {exc.strerror or exc}"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "at": now, "host": socket.gethostname()}, fh)
        return "acquired", note
    return "locked", "locked"


def _release(root: Path) -> None:
    """Remove the lock if it is still ours (a racing take-over may have
    replaced it)."""
    path = root / LOCK_NAME
    info = _read_json(path)
    if isinstance(info, dict) and info.get("pid") == os.getpid() \
            and info.get("host") == socket.gethostname():
        try:
            os.unlink(str(path))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------


def _load_list(root: Path) -> dict:
    """The list, or an empty one when there is none. ``ListError`` when it
    is no regular file (a symlink, a FIFO) or cannot be read."""
    path = root / LIST_NAME
    try:
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        return {"version": LIST_VERSION, "runs": []}
    except OSError as exc:
        raise ListError(f"list {LIST_NAME} unreadable: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(mode):
        raise ListError(f"list {LIST_NAME} is not a regular file")
    fh = open_regular(path)
    if fh is None:
        raise ListError(f"list {LIST_NAME} unreadable")
    try:
        with fh:
            data = json.loads(fh.read())
    except (OSError, ValueError) as exc:
        raise ListError(f"list {LIST_NAME} unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ListError(f"list {LIST_NAME} has no runs")
    return data


def _fresh_list_name(root: Path, now: float) -> str:
    """A list name beside a damaged list — never one that exists."""
    stem = ".mc-sink-migration." + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    name, n = f"{stem}.json", 1
    while os.path.lexists(str(root / name)):
        n += 1
        name = f"{stem}-{n}.json"
    return name


def _write_list(root: Path, data: dict, name: str = LIST_NAME) -> None:
    """Atomic: a fresh temp file next to it (``O_EXCL``, a random name — a
    symlink planted under a guessable name is never followed), then one
    rename."""
    fd, tmp = tempfile.mkstemp(prefix=f"{name}.", suffix=".tmp", dir=str(root))
    try:
        os.fchmod(fd, 0o644)  # the container's user reads it too
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, str(root / name))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _new_run(action: str, runner: str, now: float) -> dict:
    return {
        "action": action,
        "status": "started",
        "runner": runner,
        "startedAt": _iso(now),
        "python": platform.python_version(),
        "pid": os.getpid(),
    }


def _move(root: Path, src: Path, dst: Path) -> Optional[str]:
    """Rename the regular file ``src`` to ``dst`` — never over an existing
    file, never through a folder that is a symlink or a file (``_unsafe``,
    checked again after creating the target's folders). Returns the reason
    when it did not move."""
    if os.path.lexists(str(dst)):
        return "target_exists"
    try:
        mode = os.lstat(str(src)).st_mode
    except FileNotFoundError:
        return "source_missing"
    except OSError as exc:
        return f"error: {exc.strerror or exc}"
    if not stat.S_ISREG(mode):
        return "not_a_file"
    why = _unsafe(root, src) or _unsafe(root, dst)
    if why:
        return why
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        why = _unsafe(root, dst)
        if why:
            return why
        if os.path.lexists(str(dst)):
            return "target_exists"
        os.rename(str(src), str(dst))
    except OSError as exc:
        return f"error: {exc.strerror or exc}"
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def _worth_recording(moved: list, media: list, skipped: list, runner: str) -> bool:
    """A run goes into the list when it moved something — from the command
    line also when it only skipped something. A daemon run that only
    skipped would repeat at every start."""
    return bool(moved or media or (skipped and runner == "cli"))


def migrate(root: Any, dry_run: bool = False, runner: str = "cli") -> Report:
    """Move every MC copy (and its pictures) of ``root`` into ``.mc/``."""
    root = Path(root)
    report = Report(action="migrate", root=str(root), dry_run=dry_run)
    if not root.is_dir():
        report.status, report.detail = "missing", "no such sessions root"
        return report
    now = time.time()
    if dry_run or runner != "cli":
        # Read only first. A dry run ends here; the chat daemon too when
        # there is nothing to move — no lock written at every start.
        lock = root / LOCK_NAME
        if dry_run and os.path.lexists(str(lock)):
            state, why = _lock_state(lock, now)
            if state != "free":
                report.status = "locked" if state == "held" else "error"
                report.detail = why
                return report
        try:
            _load_list(root)
        except ListError as exc:
            report.status, report.detail = "error", str(exc)
            return report
        moves, media, skipped, report.omp_files = _plan(root)
        if dry_run:
            report.moved, report.media, report.skipped = moves, media, skipped
            return report
        if not moves and not media:
            report.skipped = skipped
            return report

    status, note = _acquire(root, now)
    if status != "acquired":
        report.status, report.detail = status, note
        return report
    report.detail = note
    try:
        try:
            listing = _load_list(root)
        except ListError as exc:
            report.status, report.detail = "error", str(exc)
            return report
        moves, media, skipped, report.omp_files = _plan(root)
        report.skipped = skipped
        if not _worth_recording(moves, media, skipped, runner):
            return report  # nothing to do, nothing to record
        run = _new_run("migrate", runner, now)
        run.update(moved=moves, media=media, skipped=list(skipped))
        listing["runs"].append(run)
        _write_list(root, listing)  # before the first move: a crash stays traceable
        done: list = []
        for entry in moves:
            why = _move(root, root / entry["from"], root / entry["to"])
            if why:
                skipped.append({"path": entry["from"], "reason": why})
            else:
                done.append(entry)
        done_media: list = []
        for entry in media:
            why = _move(root, root / entry["from"], root / entry["to"])
            if why:
                skipped.append({"path": entry["from"], "reason": why})
            else:
                done_media.append(entry)
        run.update(status="done", finishedAt=_iso(time.time()),
                   moved=done, media=done_media, skipped=skipped)
        _write_list(root, listing)
        report.moved, report.media, report.skipped = done, done_media, skipped
        return report
    except OSError as exc:
        report.status, report.detail = "error", f"{type(exc).__name__}: {exc.strerror or exc}"
        return report
    finally:
        _release(root)


def rollback(root: Any, all_mc: bool = False, runner: str = "cli") -> Report:
    """Undo the listed moves: each ``to`` back to its ``from`` when ``from``
    is free. ``all_mc`` also moves back MC copies written into ``.mc/``
    after the move (and ``.mc/media/*``) — the way back to an image that
    only knows the old place; it needs no list, so a damaged list does not
    block it (the run then goes into a fresh list beside the damaged one).
    Never overwrites, never deletes, never leaves the root."""
    root = Path(root)
    report = Report(action="rollback", root=str(root))
    if not root.is_dir():
        report.status, report.detail = "missing", "no such sessions root"
        return report
    now = time.time()
    status, note = _acquire(root, now)
    if status != "acquired":
        report.status, report.detail = status, note
        return report
    report.detail = note
    try:
        moved: list = []
        media: list = []
        skipped: list = []
        list_name = LIST_NAME
        try:
            listing = _load_list(root)
        except ListError as exc:
            if not all_mc:
                report.status, report.detail = "error", str(exc)
                return report
            # The way back must not depend on the list: --all finds MC's
            # copies by their header. The damaged list stays untouched.
            listing = {"version": LIST_VERSION, "runs": []}
            list_name = _fresh_list_name(root, now)
            skipped.append({"path": LIST_NAME, "reason": "list_unreadable"})
            report.detail = (f"{exc}; listed moves skipped (--all finds the copies "
                             f"anyway), this run is recorded in {list_name}")
        seen: set = set()
        for run in reversed(listing["runs"]):
            if not isinstance(run, dict) or run.get("action") != "migrate":
                continue
            for kind, out in (("moved", moved), ("media", media)):
                for entry in reversed(run.get(kind) or []):
                    if not isinstance(entry, dict) or entry.get("to") in seen:
                        continue
                    seen.add(entry.get("to"))
                    src, dst = _safe(root, entry.get("to")), _safe(root, entry.get("from"))
                    if src is None or dst is None:
                        skipped.append({"path": str(entry.get("to")), "reason": "bad_path"})
                        continue
                    why = _move(root, src, dst)
                    if why:
                        skipped.append({"path": entry["to"], "reason": why})
                        continue
                    back = {"from": entry["to"], "to": entry["from"]}
                    if kind == "moved":
                        back.update(sessionId=entry.get("sessionId"), task=bool(entry.get("task")),
                                    mixed=entry.get("mixed"))
                    out.append(back)
        if all_mc:
            for folder in _session_folders(root):
                copies = folder / MC_SINK_DIRNAME
                if not os.path.lexists(str(copies)):
                    continue
                if not _real_dir(copies):
                    skipped.append({"path": _rel(root, copies), "reason": "unsafe_target"})
                    continue
                for path in _plain_files(copies):
                    header = session_header(path) if path.name.endswith(".jsonl") else None
                    if not is_mc_header(header):
                        continue
                    dst = folder / path.name
                    mixed = mixed_lines(path)
                    why = _move(root, path, dst)
                    if why:
                        skipped.append({"path": _rel(root, path), "reason": why})
                        continue
                    moved.append({"from": _rel(root, path), "to": _rel(root, dst),
                                  "sessionId": header["id"], "task": bool(header.get("taskId")),
                                  "mixed": mixed})
                pictures = copies / MEDIA_DIRNAME
                if os.path.lexists(str(pictures)) and not _real_dir(pictures):
                    skipped.append({"path": _rel(root, pictures), "reason": "unsafe_target"})
                elif _real_dir(pictures):
                    for path in _plain_files(pictures):
                        dst = folder / MEDIA_DIRNAME / path.name
                        why = _move(root, path, dst)
                        if why:
                            skipped.append({"path": _rel(root, path), "reason": why})
                            continue
                        media.append({"from": _rel(root, path), "to": _rel(root, dst)})
        report.moved, report.media, report.skipped = moved, media, skipped
        if _worth_recording(moved, media, skipped, runner):
            run = _new_run("rollback", runner, now)
            run.update(status="done", finishedAt=_iso(time.time()), all=bool(all_mc),
                       moved=moved, media=media, skipped=skipped)
            listing["runs"].append(run)
            _write_list(root, listing, list_name)
        return report
    except OSError as exc:
        report.status, report.detail = "error", f"{type(exc).__name__}: {exc.strerror or exc}"
        return report
    finally:
        _release(root)


def remap(path: Any) -> Path:
    """A pointer to a transcript that is no longer where it was: the same
    name in ``<folder>/.mc/`` (moved), or — for a pointer into ``.mc/``
    after a rollback — in the folder above. Unchanged when the path exists
    or neither place has the file (the caller then starts over, visibly)."""
    original = Path(path)
    if original.exists():
        return original
    if original.parent.name == MC_SINK_DIRNAME:
        candidate = original.parent.parent / original.name
    else:
        candidate = original.parent / MC_SINK_DIRNAME / original.name
    return candidate if candidate.exists() else original


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sink_migration.py",
        description="Move MC's display copies of ACP sessions into <folder>/.mc/ "
                    "(and back). Run it with the agent stopped.",
    )
    parser.add_argument("--root", required=True,
                        help="the agent's sessions root (the folder that holds the "
                             "--<encoded-cwd>-- session folders)")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true",
                        help="count what would move; change nothing")
    action.add_argument("--apply", action="store_true", help="move")
    action.add_argument("--rollback", action="store_true",
                        help="move the listed copies back")
    parser.add_argument("--all", action="store_true",
                        help="with --rollback: also copies written into .mc/ after the move")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    if args.all and not args.rollback:
        parser.error("--all works only with --rollback")
    root = Path(args.root).expanduser()
    if args.rollback:
        report = rollback(root, all_mc=args.all, runner="cli")
    else:
        report = migrate(root, dry_run=args.dry_run, runner="cli")
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) if args.json
          else report.summary())
    if report.status == "ok":
        return EXIT_OK
    return EXIT_LOCKED if report.status == "locked" else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
