"""Which MC task a transcript entry names — shared by the checklist sync and
the token harvester, so both tie a session to the same task.

Two sources, both written BEFORE the work they name:
  * the omp bridge's session header of a task run (``taskId``) — every
    attempt of a task (continue nudge, reflection retry) is a new ACP session
    without the dispatch prompt, so the header is what ties it to the task;
  * the dispatch prompt in a USER turn (``**Task ID:** <uuid>``,
    ``dispatch_message_builder``) — Claude Code and omp alike.
Tool results and assistant text never count: an agent that prints a dispatch
(``mc task show``) does not start working that task.

``task_marker_role`` also says whether the session works on the task or only
checks it (review / QA dispatch) — the token harvester bills only work.
"""
from __future__ import annotations

import re
from typing import Any

DISPATCH_MARKER = re.compile(
    r"\*\*Task ID:\*\* ([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

#: Cheap substring guard for raw lines: only a line containing one of these
#: can name a task, so callers parse JSON for those lines only.
RAW_LINE_HINTS = ("Task ID:** ", '"taskId"')


def line_may_name_task(line: str) -> bool:
    return any(hint in line for hint in RAW_LINE_HINTS)


#: What the session does with the task its marker names.
ROLE_WORK = "work"
#: A review or QA dispatch: the session checks someone else's task and does
#: not build it (``dispatch_message_builder``: "# Code Review: …",
#: "# Phase Review: …", "# QA Test: …" — the marker sits under that heading).
ROLE_CHECK = "check"

_CHECK_HEADINGS = ("# Code Review: ", "# Phase Review: ", "# QA Test: ")
_HEADING = re.compile(r"^# [^\n]*", re.MULTILINE)


def task_marker(obj: dict[str, Any]) -> str | None:
    """The task id (lower-case) a transcript entry declares, or ``None``."""
    marked = task_marker_role(obj)
    return marked[0] if marked else None


def task_marker_role(obj: dict[str, Any]) -> tuple[str, str] | None:
    """``(task id lower-case, ROLE_WORK | ROLE_CHECK)`` a transcript entry
    declares, or ``None``.

    The role comes from the heading right above the marker: a review or QA
    dispatch names the task it checks, not one the session works on. A
    session header (``taskId``) carries no heading and counts as work."""
    if obj.get("type") == "session":
        task_id = obj.get("taskId")
        if isinstance(task_id, str) and task_id:
            return task_id.lower(), ROLE_WORK
        return None
    if obj.get("type") not in ("message", "user"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    for text in texts:
        if isinstance(text, str):
            match = DISPATCH_MARKER.search(text)
            if match:
                headings = _HEADING.findall(text, 0, match.start())
                is_check = bool(headings) and headings[-1].startswith(_CHECK_HEADINGS)
                return match.group(1).lower(), ROLE_CHECK if is_check else ROLE_WORK
    return None
