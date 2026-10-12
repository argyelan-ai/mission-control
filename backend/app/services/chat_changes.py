"""chat_changes — "what did THIS chat change?", per file, from the transcript.

The Changes panel's default tab. Built only from the per-edit diffs the chat
already carries (chat package P1, ``chat_diff.py``): every ``tool`` event
with ``diffs[]`` — an ACP edit's whole-file old/new text (omp over the
bridge, or omp's native details) or a Claude Edit / MultiEdit / Write. So it
works for every harness and without git: a chat in a plain folder still
shows what it changed, and nothing about another workspace is ever guessed.

Shape (``ChatChanges`` in ``frontend-v2/src/lib/chatTypes.ts``)::

    {"files": [{"path": str,            # relative to the session folder when inside it
                "additions": int, "deletions": int,
                "edits": int,           # tool calls that changed the file
                "created": bool,        # the first edit said the file was new
                "written": bool,        # at least one Write whose before is unknown
                "lastAt": str | None,   # ISO time of the newest edit
                "diff": ToolDiff}],     # all edits of the file, oldest first
     "stats": {"files": int, "additions": int, "deletions": int}}

* Newest-edited file first.
* A failed tool call (``status == "error"``) changed nothing and is skipped.
* ``additions``/``deletions`` are the NET change against what this chat
  actually left behind: the first edit's whole-file ``oldText`` (empty for
  a created file) diffed against the last edit's whole-file ``newText`` —
  not the sum of each edit's own +/-, which double-counts a line touched
  twice (create 1 -> edit 2 -> edit 3 summed to "+3 -2"; net is "+1 -0").
  Needs the raw texts (``chat_diff.capture_texts()`` around ``read_history``
  — the router does this for this endpoint only, never on an SSE/history
  path a client reads from). Falls back to the summed per-edit diffs
  (as before) whenever that is not available: no captured texts, a Claude
  ``Write`` first touching the file (predecessor unknown), or any edit
  that is a snippet (Claude ``Edit``/``MultiEdit`` — its ``old_string``/
  ``new_string`` is not the whole file, so it cannot anchor a net diff).
* The combined ``diff`` concatenates the per-edit diffs; each starts with its
  own ``hunk`` line, which the diff component renders as a separator.
* MC's own helper files (``workspace_diff.MC_HELPER_FILES``) never appear.

Pure, never raises on odd input.
"""
from __future__ import annotations

import posixpath
from typing import Any, Iterable

from app.services.chat_diff import MAX_DIFF_LINES, build_diff
from app.services.workspace_diff import is_mc_helper_file

#: Lines of the combined diff per file — a few edits' worth; the per-edit
#: cards in the chat keep the rest.
MAX_COMBINED_LINES = 3 * MAX_DIFF_LINES
#: Files listed — a chat that touched more is a refactor, not a review.
MAX_FILES = 200


def _normalise(path: str, cwd: str | None) -> str:
    """One key per file: absolute paths as they are, relative ones against
    the session folder; ``..``/``.`` folded."""
    if not path.startswith("/") and cwd:
        path = posixpath.join(cwd, path)
    return posixpath.normpath(path)


def _whole_file_text(diff: dict[str, Any], side: str) -> str | None:
    """``diff["_texts"][side]`` ("old"/"new") when it is the WHOLE file's
    text (``scope == "file"``, handed over uncut) — ``None`` otherwise (no
    captured texts at all, a snippet, or a truncated capture)."""
    texts = diff.get("_texts")
    if not isinstance(texts, dict) or texts.get("scope") != "file" or not texts.get("complete"):
        return None
    value = texts.get(side)
    return value if isinstance(value, str) else None


def _display(key: str, cwd: str | None) -> str:
    """Relative to the session folder when inside it (what the operator
    reads on a phone), otherwise the path as the agent wrote it."""
    if cwd:
        base = posixpath.normpath(cwd)
        prefix = base.rstrip("/") + "/"
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def aggregate_changes(events: Iterable[dict[str, Any]], cwd: str | None = None) -> dict[str, Any]:
    """Folds the chat's tool events into one entry per changed file."""
    entries: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    counter = 0

    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") != "tool" or ev.get("status") == "error":
            continue
        diffs = ev.get("diffs")
        if not isinstance(diffs, list):
            continue
        touched: set[str] = set()
        for diff in diffs:
            if not isinstance(diff, dict):
                continue
            raw = diff.get("path")
            if not isinstance(raw, str) or not raw.strip():
                continue
            key = _normalise(raw.strip(), cwd)
            if is_mc_helper_file(key):
                continue
            entry = entries.get(key)
            if entry is None:
                # The net diff's start: a created file's "before" is empty
                # (known); anything else needs the whole-file oldText this
                # very first edit carried — a snippet or a Write's unknown
                # predecessor leaves it ``None`` and the file falls back to
                # the summed per-edit diffs for good (set once, never
                # revisited by a later edit).
                net_old = "" if diff.get("kind") == "create" else _whole_file_text(diff, "old")
                entry = entries[key] = {
                    "path": _display(key, cwd),
                    "additions": 0,
                    "deletions": 0,
                    "edits": 0,
                    "created": diff.get("kind") == "create",
                    "written": False,
                    "lastAt": None,
                    "_lines": [],
                    "_truncated": False,
                    "_known": False,
                    "_net_old": net_old,
                    "_net_new": None,
                }
            if diff.get("kind") == "write":
                entry["written"] = True
            else:
                entry["_known"] = True
                entry["additions"] += int(diff.get("additions") or 0)
                entry["deletions"] += int(diff.get("deletions") or 0)
            # The net diff's end: the newest edit's whole-file newText, when
            # it has one — overwritten by every edit in order, so after the
            # loop it is this file's LAST edit's, or ``None`` the moment any
            # edit (e.g. a Claude snippet) does not carry one.
            entry["_net_new"] = _whole_file_text(diff, "new")
            lines = diff.get("lines") if isinstance(diff.get("lines"), list) else []
            room = MAX_COMBINED_LINES - len(entry["_lines"])
            if len(lines) > room:
                entry["_truncated"] = True
            entry["_lines"].extend(lines[: max(room, 0)])
            if diff.get("truncated"):
                entry["_truncated"] = True
            if key not in touched:
                entry["edits"] += 1
                touched.add(key)
            if isinstance(ev.get("ts"), str):
                entry["lastAt"] = ev["ts"]
            counter += 1
            order[key] = counter

    files: list[dict[str, Any]] = []
    for key in sorted(entries, key=lambda k: order[k], reverse=True)[:MAX_FILES]:
        e = entries[key]
        kind = "create" if e["created"] else ("edit" if e["_known"] else "write")
        additions, deletions = e["additions"], e["deletions"]
        if e["_net_old"] is not None and e["_net_new"] is not None:
            net = build_diff(e["path"], e["_net_old"], e["_net_new"])
            additions, deletions = (net["additions"], net["deletions"]) if net is not None else (0, 0)
        files.append({
            "path": e["path"],
            "additions": additions,
            "deletions": deletions,
            "edits": e["edits"],
            "created": e["created"],
            "written": e["written"],
            "lastAt": e["lastAt"],
            "diff": {
                "path": e["path"],
                "kind": kind,
                "lines": e["_lines"],
                "additions": additions,
                "deletions": deletions,
                "truncated": e["_truncated"],
            },
        })
    return {
        "files": files,
        "stats": {
            "files": len(files),
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
        },
    }
