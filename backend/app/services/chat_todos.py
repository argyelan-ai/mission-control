"""Harness-neutral todo snapshots for the chat view (and the task checklist).

Every coding harness MC drives keeps a small "what am I doing" list, but each
one spells it differently:

  * Claude Code calls the tool ``TodoWrite`` with the FULL list every time:
    ``{"todos": [{"content", "status", "activeForm"}]}``.
  * Codex-style harnesses call ``update_plan``:
    ``{"plan": [{"step", "status"}], "explanation"}``.
  * ACP agents (omp ``acp``) send ``session/update`` with
    ``sessionUpdate: "plan"`` and ``entries: [{"content", "status",
    "priority"}]``. The omp bridge writes that as a ``custom`` transcript
    line with ``customType: "acp-plan"`` (docker/omp-bridge/acp_chat_events.py)
    so the backend reads it from the same JSONL as everything else.

This module turns all of them into ONE event the frontend understands::

    {"kind": "todo", "uuid": str, "ts": str, "source": str,
     "items": [{"content": str,
                "status": "pending" | "in_progress" | "completed",
                "priority": "high" | "medium" | "low"   # only when given
               }]}

Every event is a complete SNAPSHOT, never a delta: the newest one per session
replaces whatever came before (all three sources already send full lists).
``source`` names where it came from (the tool name, or ``"acp_plan"``) — for
debugging only; nothing branches on it.

Hook point for further harnesses (Pi, later): a harness parser that sees a
full todo list — a tool call, an extension event, a protocol message — calls
:func:`todo_event` with the raw item list and its own ``source`` name. The
item reader below already accepts ``content``/``step``/``title``/``text`` as
the item text and the common status spellings, so a new harness usually
needs no change here at all. A harness that only sends DELTAS (omp's native
``todo`` tool with ``op``) is deliberately NOT mapped: a snapshot built from
deltas we may have missed would claim a list the agent never had.

Pure functions, never raise: an unreadable list yields ``None`` (no event),
the same policy as every transcript parser (formats change without notice).
"""
from __future__ import annotations

import re
from typing import Any

#: Tool names whose input is a full todo list. Compared case-insensitively
#: after removing ``_``/``-`` so ``TodoWrite``, ``todo_write`` and
#: ``todo-write`` are one name.
_TODO_TOOL_KEYS = frozenset({"todowrite", "updateplan"})

#: Where the list lives inside the tool input, in order of preference.
_LIST_KEYS = ("todos", "plan", "entries", "items")

#: Where the item text lives inside one entry, in order of preference.
_TEXT_KEYS = ("content", "step", "title", "text")

_STATUS = {
    "pending": "pending",
    "todo": "pending",
    "not_started": "pending",
    "open": "pending",
    "in_progress": "in_progress",
    "inprogress": "in_progress",
    "in-progress": "in_progress",
    "active": "in_progress",
    "running": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
}

_PRIORITIES = frozenset({"high", "medium", "low"})

#: A list longer than this is cut — the panel is a glance, not a backlog.
MAX_ITEMS = 50
#: One item's text is cut at this length (an item is one line of intent).
MAX_TEXT_LEN = 300

ACP_PLAN_SOURCE = "acp_plan"
#: customType the omp bridge uses for a forwarded ACP plan update.
ACP_PLAN_CUSTOM_TYPE = "acp-plan"


def is_todo_tool(name: Any) -> bool:
    """True when a tool with this name carries a full todo list."""
    if not isinstance(name, str):
        return False
    key = re.sub(r"[_\-\s]", "", name).lower()
    return key in _TODO_TOOL_KEYS


def normalize_items(raw: Any) -> list[dict[str, str]] | None:
    """Raw entry list -> normalised items, or ``None`` when ``raw`` is not a
    list at all. An empty list stays an empty list: it is a real snapshot
    ("the agent cleared its list"), not a missing one. Entries without text
    are dropped; an unknown status counts as ``pending`` (an item we cannot
    read as done or running is still open)."""
    if not isinstance(raw, list):
        return None
    items: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"content": entry}
        if not isinstance(entry, dict):
            continue
        text = ""
        for key in _TEXT_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            continue
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN] + "…"
        status_raw = entry.get("status")
        status = _STATUS.get(status_raw.strip().lower(), "pending") if isinstance(status_raw, str) else "pending"
        item: dict[str, str] = {"content": text, "status": status}
        priority = entry.get("priority")
        if isinstance(priority, str) and priority.strip().lower() in _PRIORITIES:
            item["priority"] = priority.strip().lower()
        items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    return items


def todo_event(raw_items: Any, *, uuid: Any, ts: Any, source: str) -> dict[str, Any] | None:
    """The one constructor every harness goes through (see module docstring:
    this is the hook point for a new harness)."""
    if not uuid or not isinstance(ts, str) or not ts:
        return None
    items = normalize_items(raw_items)
    if items is None:
        return None
    return {"kind": "todo", "uuid": uuid, "ts": ts, "source": source, "items": items}


def todo_event_from_tool(name: Any, tool_input: Any, *, uuid: Any, ts: Any) -> dict[str, Any] | None:
    """A tool call -> a todo snapshot, or ``None`` when the tool is not a
    todo tool or carries no readable list."""
    if not is_todo_tool(name) or not isinstance(tool_input, dict):
        return None
    for key in _LIST_KEYS:
        if key in tool_input:
            return todo_event(tool_input.get(key), uuid=uuid, ts=ts, source=str(name))
    return None


def todo_event_from_acp_plan(entries: Any, *, uuid: Any, ts: Any) -> dict[str, Any] | None:
    """An ACP ``plan`` update's ``entries`` -> a todo snapshot."""
    return todo_event(entries, uuid=uuid, ts=ts, source=ACP_PLAN_SOURCE)


def latest_todo(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest todo snapshot in a list of parsed events (latest wins)."""
    for ev in reversed(events):
        if ev.get("kind") == "todo":
            return ev
    return None
