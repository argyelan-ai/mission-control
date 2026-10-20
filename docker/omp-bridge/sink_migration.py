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
``<folder>/.mc/media/``. Same file system, so each move is one atomic rename
that keeps the modification time (the backend ranks sessions by it).

WHAT IT NEVER DOES: delete or overwrite (a target that exists is skipped, the
reason goes into the list); touch any subfolder (``previews/``, omp's
artifact folders — also those with MC's name pattern, live
``2026-09-16T01-06-13_<id>/``), any dot file (omp's ``.<name>.jsonl.lock.os``),
``acp-chat-state.json``, ``acp-chat.json`` or omp's own files; create a
``.bak`` name (omp renames ``*.jsonl.<x>.bak`` back into sessions).

THE LIST: ``<root>/.mc-sink-migration.json`` — never ``.jsonl``, the
backend's ``*.jsonl`` search would read it as a session. Every run that moved
something (and every run from the command line) is appended by rewriting the
file atomically; paths are relative to the root, because the host and the
container see the same tree under different absolute paths. A run is written
BEFORE its moves (status ``started``) and again after (``done``): a crash in
between still leaves a list ``rollback`` can follow.

THE LOCK: ``<root>/.mc-sink-migration.lock`` (``O_EXCL``) with pid, time and
host. A lock older than ``LOCK_STALE_S`` or of a dead process on the same
host is taken over; a fresh one stops the run (status ``locked``) — the
chat daemon then says so in the chat.

WHO RUNS IT: the operator, on the host, in the deploy window with the agent
stopped — ``python3 sink_migration.py --root DIR --dry-run`` (counts only),
then ``--apply``; ``--rollback [--all]`` is the way back. As a safety net the
chat daemon also runs ``migrate`` in ``ChatSession.start()`` before it reads
its pointer, and ``remap`` sends an old pointer to the moved file.

Standard library only; runs on Python 3.9 (the host) and 3.11 (the image).
Keep ``MC_SINK_DIRNAME`` equal to ``acp_chat_events.MC_SINK_DIRNAME``
(tests/test_sink_migration.py checks it) — this file stays importable and
runnable on its own.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: Where MC's copies live, below each session folder.
MC_SINK_DIRNAME = ".mc"
#: The list of every move (at the sessions root). ``.json`` on purpose.
LIST_NAME = ".mc-sink-migration.json"
#: The lock (at the sessions root).
LOCK_NAME = ".mc-sink-migration.lock"
#: A lock older than this is taken over (seconds). A real run takes well
#: under a second; ten minutes only guards against a run that died.
LOCK_STALE_S = 600
LIST_VERSION = 1
MEDIA_DIRNAME = "media"
#: Files in a session folder that are MC's but never move.
NEVER_TOUCH = frozenset({"acp-chat-state.json", "acp-chat.json"})
#: MC's line ids start with this (acp_chat_events._ID_PREFIX); omp's are hex.
_MC_ID_PREFIX = "acp"
#: Line types only omp writes — a sign that omp wrote into MC's copy.
_OMP_ONLY_TYPES = frozenset({"model_change", "thinking_level_change", "session_exit"})
_HEADER_BYTES = 65536

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


def session_header(path: Path) -> Optional[dict]:
    """The session header omp would accept — line 1, or line 2 after a
    ``{"type": "title"}`` line, with ``type == "session"`` and a string
    ``id`` — or None. Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = [fh.readline(_HEADER_BYTES), fh.readline(_HEADER_BYTES)]
    except OSError:
        return None
    parsed = []
    for line in raw:
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        parsed.append(value if isinstance(value, dict) else None)
    header = parsed[1] if parsed[0] is not None and parsed[0].get("type") == "title" else parsed[0]
    if header is None or header.get("type") != "session" or not isinstance(header.get("id"), str):
        return None
    return header


def _is_mc(header: Optional[dict]) -> bool:
    return header is not None and header.get("bridge") == "acp"


def mixed_lines(path: Path) -> Optional[dict]:
    """Lines omp wrote into MC's copy (a "mixed file", live 16.09.2026):
    ``{"foreignLines": n, "types": [...]}``, or None when there are none.
    A line is omp's when its ``id`` is not MC's (``acp…``) or its type is
    one only omp writes."""
    count = 0
    types: set = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
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
    except OSError:
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
        for path in _plain_files(folder):
            if not path.name.endswith(".jsonl") or path.name in NEVER_TOUCH:
                continue
            header = session_header(path)
            if header is None:
                continue  # no session at all: neither omp's nor ours
            if not _is_mc(header):
                omp_files += 1
                continue
            target = folder / MC_SINK_DIRNAME / path.name
            if os.path.lexists(target):
                skipped.append({"path": _rel(root, path), "reason": "target_exists"})
                continue
            moves.append({
                "from": _rel(root, path),
                "to": _rel(root, target),
                "sessionId": header["id"],
                "task": bool(header.get("taskId")),
                "mixed": mixed_lines(path),
            })
        pictures = folder / MEDIA_DIRNAME
        if pictures.is_dir() and not pictures.is_symlink():
            for path in _plain_files(pictures):
                target = folder / MC_SINK_DIRNAME / MEDIA_DIRNAME / path.name
                if os.path.lexists(target):
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


def _lock_state(path: Path, now: float) -> tuple:
    """(held, why) for an EXISTING lock file. Judged by its age first; a
    lock of the same host is also free when its process is gone. A lock of
    another host (host vs. container) can only be judged by age."""
    try:
        raw = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False, "lock vanished"
    except OSError as exc:
        return True, f"lock unreadable: {exc.strerror or exc}"
    try:
        info = json.loads(raw)
    except ValueError:
        info = None
    if not isinstance(info, dict):
        age = now - mtime
        if age > LOCK_STALE_S:
            return False, f"stale lock without content (age {int(age)} s)"
        return True, f"locked (lock without content, age {int(age)} s)"
    at = info.get("at")
    age = now - (at if isinstance(at, (int, float)) else mtime)
    pid = info.get("pid")
    host = info.get("host")
    if age > LOCK_STALE_S:
        return False, f"stale lock of pid {pid} (age {int(age)} s)"
    if host == socket.gethostname() and isinstance(pid, int) and not _pid_alive(pid):
        return False, f"stale lock of pid {pid} (process gone)"
    return True, f"locked by pid {pid} since {int(age)} s"


def _acquire(root: Path, now: float) -> tuple:
    """(acquired, note). Takes over a stale lock once."""
    path = root / LOCK_NAME
    note = ""
    for attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            held, why = _lock_state(path, now)
            if held or attempt:
                return False, why
            try:
                os.unlink(str(path))  # a lock holds no data; a stale one is ours to clear
            except FileNotFoundError:
                pass
            except OSError as exc:
                return False, f"stale lock not removable: {exc.strerror or exc}"
            note = f"took over {why}"
            continue
        except OSError as exc:
            return False, f"lock not creatable: {exc.strerror or exc}"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "at": now, "host": socket.gethostname()}, fh)
        return True, note
    return False, "locked"


def _release(root: Path) -> None:
    """Remove the lock if it is still ours (a racing take-over may have
    replaced it)."""
    path = root / LOCK_NAME
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
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
    path = root / LIST_NAME
    if not os.path.lexists(path):
        return {"version": LIST_VERSION, "runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ListError(f"list {LIST_NAME} unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ListError(f"list {LIST_NAME} has no runs")
    return data


def _write_list(root: Path, data: dict) -> None:
    """Atomic: a temp file next to it, then one rename."""
    path = root / LIST_NAME
    tmp = root / f"{LIST_NAME}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            os.unlink(str(tmp))
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


def _move(src: Path, dst: Path) -> Optional[str]:
    """Rename ``src`` to ``dst`` — never over an existing file. Returns the
    reason when it did not move."""
    if os.path.lexists(dst):
        return "target_exists"
    if not os.path.lexists(src):
        return "source_missing"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(src), str(dst))
    except OSError as exc:
        return f"error: {exc.strerror or exc}"
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def migrate(root: Any, dry_run: bool = False, runner: str = "cli") -> Report:
    """Move every MC copy (and its pictures) of ``root`` into ``.mc/``."""
    root = Path(root)
    report = Report(action="migrate", root=str(root), dry_run=dry_run)
    if not root.is_dir():
        report.status, report.detail = "missing", "no such sessions root"
        return report
    now = time.time()
    if dry_run:
        lock = root / LOCK_NAME
        if os.path.lexists(lock):
            held, why = _lock_state(lock, now)
            if held:
                report.status, report.detail = "locked", why
                return report
        try:
            _load_list(root)
        except ListError as exc:
            report.status, report.detail = "error", str(exc)
            return report
        report.moved, report.media, report.skipped, report.omp_files = _plan(root)
        return report

    acquired, note = _acquire(root, now)
    if not acquired:
        report.status, report.detail = "locked", note
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
        if not moves and not media and not (skipped and runner == "cli"):
            return report  # nothing to do, nothing to record
        run = _new_run("migrate", runner, now)
        run.update(moved=moves, media=media, skipped=list(skipped))
        listing["runs"].append(run)
        _write_list(root, listing)  # before the first move: a crash stays traceable
        done: list = []
        for entry in moves:
            why = _move(root / entry["from"], root / entry["to"])
            if why:
                skipped.append({"path": entry["from"], "reason": why})
            else:
                done.append(entry)
        done_media: list = []
        for entry in media:
            why = _move(root / entry["from"], root / entry["to"])
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
    only knows the old place. Never overwrites, never deletes."""
    root = Path(root)
    report = Report(action="rollback", root=str(root))
    if not root.is_dir():
        report.status, report.detail = "missing", "no such sessions root"
        return report
    now = time.time()
    acquired, note = _acquire(root, now)
    if not acquired:
        report.status, report.detail = "locked", note
        return report
    report.detail = note
    try:
        try:
            listing = _load_list(root)
        except ListError as exc:
            report.status, report.detail = "error", str(exc)
            return report
        moved: list = []
        media: list = []
        skipped: list = []
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
                    why = _move(src, dst)
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
                if not copies.is_dir() or copies.is_symlink():
                    continue
                for path in _plain_files(copies):
                    header = session_header(path) if path.name.endswith(".jsonl") else None
                    if not _is_mc(header):
                        continue
                    dst = folder / path.name
                    mixed = mixed_lines(path)
                    why = _move(path, dst)
                    if why:
                        skipped.append({"path": _rel(root, path), "reason": why})
                        continue
                    moved.append({"from": _rel(root, path), "to": _rel(root, dst),
                                  "sessionId": header["id"], "task": bool(header.get("taskId")),
                                  "mixed": mixed})
                pictures = copies / MEDIA_DIRNAME
                if pictures.is_dir() and not pictures.is_symlink():
                    for path in _plain_files(pictures):
                        dst = folder / MEDIA_DIRNAME / path.name
                        why = _move(path, dst)
                        if why:
                            skipped.append({"path": _rel(root, path), "reason": why})
                            continue
                        media.append({"from": _rel(root, path), "to": _rel(root, dst)})
        report.moved, report.media, report.skipped = moved, media, skipped
        if moved or media or runner == "cli":
            run = _new_run("rollback", runner, now)
            run.update(status="done", finishedAt=_iso(time.time()), all=bool(all_mc),
                       moved=moved, media=media, skipped=skipped)
            listing["runs"].append(run)
            _write_list(root, listing)
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
