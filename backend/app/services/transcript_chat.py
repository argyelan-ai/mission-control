"""Transcript chat parser — normalizes Claude Code JSONL lines into chat events.

Pure functions only (no I/O, no DB, no framework deps) — consumed by A3/A4 to
build the per-session chat view and mirrored in the frontend's chatTypes.ts.

Normalized event shapes (plain dicts, JSON-serializable):
  {"kind":"message","uuid":str,"ts":str,"role":"user"|"assistant","text":str,
   "model":str|None,"sidechain":bool}
  {"kind":"tool","uuid":str,"ts":str,"name":str,"title":str,"detail":dict,
   "result":str|None,"status":"done"|"error","stats":{"additions":int,"deletions":int}|None,
   "sidechain":bool}
  {"kind":"thinking","uuid":str,"ts":str,"text":str,"sidechain":bool}
  {"kind":"command","uuid":str,"ts":str,"command":str}
  {"kind":"usage","uuid":str,"ts":str,"inputTokens":int,"outputTokens":int,
   "model":str|None,"effort":str|None}

`parse_transcript_line` also emits an internal ``_tool_result`` event for
``tool_result`` content blocks (type=="user" lines) — A3 merges these onto
their matching ``tool`` event by ``tool_use_id``; they never reach the
frontend on their own.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("mc.transcript_chat")

_DETAIL_TRUNCATE_LEN = 2000
_TITLE_MAX_LEN = 80

# Tools whose title is built from a file_path basename, prefixed "Read".
_FILE_PATH_READ_TOOLS = {"Read", "NotebookEdit"}


def parse_transcript_line(line: str) -> list[dict[str, Any]]:
    """One raw JSONL line -> 0..n normalized chat events. Never raises."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        logger.debug("transcript_chat: malformed JSON line, skipping")
        return []

    if not isinstance(d, dict):
        logger.debug("transcript_chat: line is not a JSON object, skipping")
        return []

    entry_type = d.get("type")
    try:
        if entry_type == "user":
            return _parse_user_entry(d)
        if entry_type == "assistant":
            return _parse_assistant_entry(d)
    except Exception:
        logger.debug("transcript_chat: failed to parse %s entry", entry_type, exc_info=True)
        return []

    return []


def _parse_user_entry(d: dict[str, Any]) -> list[dict[str, Any]]:
    msg_uuid = d.get("uuid")
    ts = d.get("timestamp")
    message = d.get("message")
    if not msg_uuid or not ts or not message:
        return []

    content = message.get("content")
    if not isinstance(content, list):
        return []

    sidechain = bool(d.get("isSidechain", False))
    events: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text")
            if text is None:
                continue
            if text.startswith("/") and "\n" not in text:
                events.append(
                    {
                        "kind": "command",
                        "uuid": msg_uuid,
                        "ts": ts,
                        "command": text,
                    }
                )
            else:
                events.append(
                    {
                        "kind": "message",
                        "uuid": msg_uuid,
                        "ts": ts,
                        "role": "user",
                        "text": text,
                        "model": None,
                        "sidechain": sidechain,
                    }
                )
        elif block_type == "tool_result":
            events.append(
                {
                    "kind": "_tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": block.get("content"),
                }
            )

    return events


def _parse_assistant_entry(d: dict[str, Any]) -> list[dict[str, Any]]:
    msg_uuid = d.get("uuid")
    ts = d.get("timestamp")
    message = d.get("message")
    if not msg_uuid or not ts or not message:
        return []

    content = message.get("content")
    if not isinstance(content, list):
        return []

    sidechain = bool(d.get("isSidechain", False))
    model = message.get("model")
    events: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text")
            if text is None:
                continue
            events.append(
                {
                    "kind": "message",
                    "uuid": msg_uuid,
                    "ts": ts,
                    "role": "assistant",
                    "text": text,
                    "model": model,
                    "sidechain": sidechain,
                }
            )
        elif block_type == "thinking":
            text = block.get("thinking")
            if text is None:
                continue
            events.append(
                {
                    "kind": "thinking",
                    "uuid": msg_uuid,
                    "ts": ts,
                    "text": text,
                    "sidechain": sidechain,
                }
            )
        elif block_type == "tool_use":
            name = block.get("name")
            if not name:
                continue
            tool_input = block.get("input") or {}
            events.append(
                {
                    "kind": "tool",
                    "uuid": msg_uuid,
                    "ts": ts,
                    "name": name,
                    "title": build_tool_title(name, tool_input),
                    "detail": _truncate_detail(tool_input),
                    "result": None,
                    "status": "done",
                    "stats": None,
                    "sidechain": sidechain,
                }
            )

    usage = message.get("usage")
    if usage:
        input_tokens = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        )
        events.append(
            {
                "kind": "usage",
                "uuid": msg_uuid,
                "ts": ts,
                "inputTokens": input_tokens,
                "outputTokens": usage.get("output_tokens") or 0,
                "model": model,
                "effort": d.get("effort"),
            }
        )

    return events


def _truncate_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Truncates string values longer than 2000 chars, appending an ellipsis."""
    truncated: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, str) and len(value) > _DETAIL_TRUNCATE_LEN:
            truncated[key] = value[:_DETAIL_TRUNCATE_LEN] + "…"
        else:
            truncated[key] = value
    return truncated


def build_tool_title(name: str, tool_input: dict[str, Any]) -> str:
    """Builds a short, human-readable title for a tool_use event.

    'Read' + {'file_path': '/x/y.py'} -> 'Read y.py' etc.
    """
    if name in _FILE_PATH_READ_TOOLS:
        title = f"Read {_basename(tool_input.get('file_path'))}"
    elif name == "Write":
        title = f"Write {_basename(tool_input.get('file_path'))}"
    elif name == "Edit":
        title = f"Edit {_basename(tool_input.get('file_path'))}"
    elif name == "Bash":
        title = f"$ {tool_input.get('command', '')}"
    elif name in ("Grep", "Glob"):
        title = f'Search "{tool_input.get("pattern", "")}"'
    elif name == "WebSearch":
        title = f'Web "{tool_input.get("query", "")}"'
    elif name == "WebFetch":
        title = f"Fetch {_domain(tool_input.get('url'))}"
    elif name in ("Task", "Agent"):
        title = f"Agent: {tool_input.get('description', '')}"
    else:
        title = name

    return _truncate_title(title)


def _basename(file_path: Any) -> str:
    if not file_path:
        return ""
    return str(file_path).rstrip("/").rsplit("/", 1)[-1]


def _domain(url: Any) -> str:
    if not url:
        return ""
    stripped = str(url).split("://", 1)[-1]
    return stripped.split("/", 1)[0]


def _truncate_title(title: str) -> str:
    if len(title) <= _TITLE_MAX_LEN:
        return title
    return title[: _TITLE_MAX_LEN - 1] + "…"
