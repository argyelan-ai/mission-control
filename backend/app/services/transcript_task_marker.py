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


def task_marker(obj: dict[str, Any]) -> str | None:
    """The task id (lower-case) a transcript entry declares, or ``None``."""
    if obj.get("type") == "session":
        task_id = obj.get("taskId")
        return task_id.lower() if isinstance(task_id, str) and task_id else None
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
                return match.group(1).lower()
    return None
