"""Transcript chat parser — normalizes Claude Code JSONL lines into chat events.

Pure functions only (no I/O, no DB, no framework deps) — consumed by A3/A4 to
build the per-session chat view and mirrored in the frontend's chatTypes.ts.

Normalized event shapes (plain dicts, JSON-serializable):
  {"kind":"message","uuid":str,"ts":str,"role":"user"|"assistant","text":str,
   "model":str|None,"sidechain":bool}
  {"kind":"tool","uuid":str,"ts":str,"name":str,"title":str,"detail":dict,
   "toolUseId":str|None,"result":str|None,"status":"done"|"error",
   "stats":{"additions":int,"deletions":int}|None,"sidechain":bool}
  {"kind":"thinking","uuid":str,"ts":str,"text":str,"sidechain":bool}
  {"kind":"command","uuid":str,"ts":str,"command":str}
  {"kind":"usage","uuid":str,"ts":str,"inputTokens":int,"outputTokens":int,
   "model":str|None,"effort":str|None}

`parse_transcript_line` also emits an internal ``_tool_result`` event for
``tool_result`` content blocks (type=="user" lines) — ``{"kind":"_tool_result",
"tool_use_id":str,"content":Any,"is_error":bool}``. ``read_history`` merges
these onto their matching ``tool`` event by ``tool_use_id`` == ``toolUseId``
(needed to disambiguate parallel tool calls within one assistant turn);
they never reach the frontend on their own.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services import sse
from app.redis_client import RedisKeys
from app.services.token_harvester import _host_home, _should_attribute_boss_path

logger = logging.getLogger("mc.transcript_chat")

_DETAIL_TRUNCATE_LEN = 2000
_TITLE_MAX_LEN = 80

# Session-scan limits (find_active_session / transcript_allowed)
_LIVE_WINDOW_SECONDS = 60
_BOSS_SCAN_LINES = 20

# Host-runtime slugs that resolve to the Boss's own ~/.claude session dir —
# every other host agent (Hermes, Jarvis) has no transcript at all.
_BOSS_SLUGS = ("boss", "boss-host")

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
                    "is_error": bool(block.get("is_error", False)),
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
                    "toolUseId": block.get("id"),
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


# ── Session resolution (I/O — reads transcript dirs, not pure) ──────────────
#
# The functions below are the only I/O-touching code in this module (the
# parser above stays pure). They locate an agent's live Claude Code session
# on disk and gate Boss/host transcripts against Mark's private ~/.claude
# sessions before anything from them reaches the frontend.


def encode_cwd(cwd: str) -> str:
    """Replicates Claude Code's own project-directory name encoding: every
    non-alphanumeric character (including path separators and dots) becomes
    a literal '-'. Verified against a real session dir name, see tests."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def resolve_transcript_dir(agent) -> Path | None:
    """Maps an Agent to the on-disk directory holding its Claude Code JSONL
    transcripts, or None if this agent/runtime has no transcript at all
    (Hermes, Jarvis, manual agents — anything not driven by the claude CLI).

    Duck-typed on ``agent.slug`` / ``agent.agent_runtime`` so tests can pass
    a plain stub instead of a DB-backed Agent row.
    """
    slug = getattr(agent, "slug", None)
    runtime = getattr(agent, "agent_runtime", None)
    if not slug:
        return None

    if runtime == "cli-bridge":
        return (
            _host_home()
            / ".mc"
            / "agents"
            / slug
            / "claude-config"
            / "projects"
            / encode_cwd("/home/agent")
        )

    if runtime == "host" and slug in _BOSS_SLUGS:
        checkout = str(_host_home() / ".mc" / "checkouts" / "mission-control")
        return _host_home() / ".claude" / "projects" / encode_cwd(checkout)

    return None


def find_active_session(tdir: Path) -> tuple[Path, dict[str, Any]] | None:
    """Finds the newest ``*.jsonl`` transcript directly under ``tdir`` (does
    NOT recurse into subdirectories — those hold sidechains/artifacts, not
    top-level sessions).

    Returns ``(path, meta)`` where ``meta`` is
    ``{"sessionId": <filename stem>, "mtime": <iso8601>, "live": <bool>}``
    — ``live`` is True when the file was written within the last
    ``_LIVE_WINDOW_SECONDS``. Returns None if the directory doesn't exist or
    has no top-level jsonl files.
    """
    if not tdir.is_dir():
        return None

    newest_path: Path | None = None
    newest_mtime = -1.0
    for candidate in tdir.glob("*.jsonl"):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest_path = candidate

    if newest_path is None:
        return None

    meta = {
        "sessionId": newest_path.stem,
        "mtime": datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat(),
        "live": (time.time() - newest_mtime) < _LIVE_WINDOW_SECONDS,
    }
    return newest_path, meta


def _extract_cwd_and_branch(path: Path) -> tuple[str, str | None]:
    """Scans the first ``_BOSS_SCAN_LINES`` lines of a transcript for the
    first line carrying a top-level ``cwd`` — Claude Code stamps ``cwd`` /
    ``gitBranch`` on every line, so line 1 normally suffices; the 20-line
    cap is a safety margin against odd/legacy transcripts, not an expected
    scan depth. Returns ("", None) if nothing usable was found."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= _BOSS_SCAN_LINES:
                break
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(d, dict) or "cwd" not in d:
                continue
            return d.get("cwd") or "", d.get("gitBranch")
    return "", None


def transcript_allowed(agent, path: Path) -> bool:
    """Privacy gate: cli-bridge agent transcripts are always MC's own
    workspace (Docker containers have nothing else to write) — always
    allowed. Host-runtime Boss transcripts live in Mark's own ~/.claude,
    shared with his private/personal sessions — only lines that look like MC
    work (mission-control cwd or a task/ branch) are allowed through, via
    the same heuristic token_harvester uses for cost attribution. Any read
    failure (missing/unreadable file) fails closed.

    Explicitly gated on ``slug in _BOSS_SLUGS`` (not just "any non-cli-bridge
    agent") — resolve_transcript_dir() already returns None for every other
    host agent (Hermes, Jarvis) so this branch should never be reached for
    them in practice, but a caller passing one directly with an arbitrary
    path must still fail closed rather than accidentally running the Boss
    heuristic against it (review finding, fix-round 1)."""
    runtime = getattr(agent, "agent_runtime", None)
    slug = getattr(agent, "slug", None)

    if runtime == "cli-bridge":
        return True
    if not (runtime == "host" and slug in _BOSS_SLUGS):
        return False

    try:
        cwd, git_branch = _extract_cwd_and_branch(path)
    except OSError:
        return False

    return _should_attribute_boss_path(cwd, git_branch)


# ── History reading (I/O — reads a transcript file, not pure) ───────────────

_RESULT_TRUNCATE_LEN = 4000
_STATS_TOOLS = ("Edit", "Write")


def read_history(path: Path, limit: int = 200, before_uuid: str | None = None) -> dict[str, Any]:
    """Reads a transcript file top-to-bottom and returns one page of chat
    events plus session metadata.

    Streams the file line-by-line (transcripts can grow large over a long
    session). Dedups on the top-level entry ``uuid`` — Claude Code can repeat
    a line verbatim across a resumed session, and re-parsing it would
    duplicate every event derived from it. Internal ``_tool_result`` events
    are merged onto the ``tool`` event with the matching ``toolUseId`` (never
    appended to ``events`` on their own) — matching by id rather than
    position is what lets a multi-tool assistant turn resolve correctly, see
    the module docstring. ``sidechain`` events are left inline; the frontend
    groups them.

    Without ``before_uuid``, returns the newest ``limit`` events (initial
    load). With ``before_uuid``, returns the ``limit`` events immediately
    preceding the first occurrence of that uuid (backward paging) — all
    events sharing one entry's uuid are contiguous, so "first occurrence" is
    that entry's start and excludes the whole entry from the page, never
    just part of it. An unknown ``before_uuid`` yields an empty page.
    """
    session_id = path.stem
    try:
        live = (time.time() - path.stat().st_mtime) < _LIVE_WINDOW_SECONDS
    except OSError:
        live = False

    started_at: str | None = None
    events: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()
    tool_events_by_id: dict[str, dict[str, Any]] = {}

    try:
        lines_file = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        lines_file = None

    if lines_file is not None:
        with lines_file:
            for raw_line in lines_file:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    d = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue

                entry_uuid = d.get("uuid")
                if entry_uuid is not None:
                    if entry_uuid in seen_uuids:
                        continue
                    seen_uuids.add(entry_uuid)

                if started_at is None and d.get("timestamp"):
                    started_at = d["timestamp"]

                for ev in parse_transcript_line(raw_line):
                    if ev["kind"] == "_tool_result":
                        tool_ev = tool_events_by_id.get(ev.get("tool_use_id"))
                        if tool_ev is not None:
                            _merge_tool_result(tool_ev, ev)
                        continue

                    if ev["kind"] == "tool":
                        stats = _compute_edit_stats(ev["name"], ev["detail"])
                        if stats is not None:
                            ev["stats"] = stats
                        tool_use_id = ev.get("toolUseId")
                        if tool_use_id is not None:
                            tool_events_by_id[tool_use_id] = ev

                    events.append(ev)

    total = len(events)
    if before_uuid is not None:
        cut = next((i for i, e in enumerate(events) if e.get("uuid") == before_uuid), None)
        if cut is None:
            page: list[dict[str, Any]] = []
            has_more = False
        else:
            start = max(0, cut - limit)
            page = events[start:cut]
            has_more = start > 0
    else:
        start = max(0, total - limit)
        page = events[start:]
        has_more = start > 0

    return {
        "events": page,
        "session": {"sessionId": session_id, "live": live, "startedAt": started_at},
        "hasMore": has_more,
    }


def _merge_tool_result(tool_event: dict[str, Any], tool_result: dict[str, Any]) -> None:
    """Merges an internal ``_tool_result`` event onto its matching ``tool``
    event (already looked up by ``toolUseId`` before this is called)."""
    content = tool_result.get("content")
    tool_event["result"] = str(content)[:_RESULT_TRUNCATE_LEN]
    if tool_result.get("is_error"):
        tool_event["status"] = "error"


def _compute_edit_stats(name: str, detail: dict[str, Any]) -> dict[str, int] | None:
    """Edit tool_use inputs carry ``old_string``/``new_string`` — a naive
    line count of each, not a real diff, but enough for a chat summary
    badge. None when neither field is present (e.g. Write's ``content``, or
    any non-Edit/Write tool)."""
    if name not in _STATS_TOOLS:
        return None
    old_string = detail.get("old_string")
    new_string = detail.get("new_string")
    if old_string is None and new_string is None:
        return None
    return {
        "additions": _count_lines(new_string),
        "deletions": _count_lines(old_string),
    }


def _count_lines(value: Any) -> int:
    if not value:
        return 0
    return str(value).count("\n") + 1


# ── Live tailing (I/O — background polling task, not pure) ──────────────────


class ChatTailerManager:
    """Refcounted, per-agent background poller that follows a live Claude
    Code transcript and republishes each new line as a ``chat_event`` SSE
    frame on ``RedisKeys.agent_chat_channel(agent_id)``.

    One asyncio task per agent, shared across every connected SSE client for
    that agent (``acquire``/``release`` refcount it) — N browser tabs on the
    same agent's chat never spawn N pollers. The task is cancelled the moment
    the last client disconnects, so an agent nobody is watching costs nothing.

    Applies the same merge/skip semantics as ``read_history``: an internal
    ``_tool_result`` event is never published on its own — instead, when it
    arrives, the already-published ``tool`` event (matched by ``toolUseId``)
    is mutated in place and *republished* under the same ``uuid``/
    ``toolUseId``, so the frontend reducer replaces its existing tool card
    instead of appending a second one.
    """

    POLL_INTERVAL = 1.0

    def __init__(self) -> None:
        self._refcounts: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def acquire(self, agent_id: str, path: Path) -> None:
        """Registers one more client for ``agent_id``. Starts the poll task
        if this is the first client; otherwise just bumps the refcount — the
        already-running task keeps tailing from wherever it is."""
        count = self._refcounts.get(agent_id, 0)
        self._refcounts[agent_id] = count + 1
        if count == 0:
            self._tasks[agent_id] = asyncio.create_task(self._run(agent_id, path))

    async def release(self, agent_id: str) -> None:
        """Drops one client for ``agent_id``. Cancels and awaits the poll
        task once the last client releases."""
        count = self._refcounts.get(agent_id, 0)
        if count <= 1:
            self._refcounts.pop(agent_id, None)
            task = self._tasks.pop(agent_id, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        else:
            self._refcounts[agent_id] = count - 1

    async def _run(self, agent_id: str, initial_path: Path) -> None:
        channel = RedisKeys.agent_chat_channel(agent_id)
        tdir = initial_path.parent
        current_path = initial_path
        offset = 0
        buffer = ""
        tool_events_by_id: dict[str, dict[str, Any]] = {}

        while True:
            await asyncio.sleep(self.POLL_INTERVAL)

            try:
                active = find_active_session(tdir)
            except OSError:
                active = None
            if active is not None and active[0] != current_path:
                current_path = active[0]
                offset = 0
                buffer = ""
                tool_events_by_id = {}
                await sse.broadcast(channel, "chat_event", {"kind": "session_changed"})
                continue

            try:
                size = current_path.stat().st_size
            except OSError:
                # File disappeared (rotated/deleted mid-session) — state is
                # unknown, but the directory keeps getting polled so a
                # replacement (or the same path reappearing) is picked up.
                continue

            if size <= offset:
                continue

            try:
                with current_path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
            except OSError:
                continue

            offset = size
            buffer += chunk
            lines = buffer.split("\n")
            buffer = lines.pop()  # last element: partial line (or "") — held for next tick

            for raw_line in lines:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                for ev in parse_transcript_line(raw_line):
                    if ev["kind"] == "_tool_result":
                        tool_ev = tool_events_by_id.get(ev.get("tool_use_id"))
                        if tool_ev is not None:
                            _merge_tool_result(tool_ev, ev)
                            await sse.broadcast(channel, "chat_event", tool_ev)
                        continue

                    if ev["kind"] == "tool":
                        stats = _compute_edit_stats(ev["name"], ev["detail"])
                        if stats is not None:
                            ev["stats"] = stats
                        tool_use_id = ev.get("toolUseId")
                        if tool_use_id is not None:
                            tool_events_by_id[tool_use_id] = ev

                    await sse.broadcast(channel, "chat_event", ev)


tailer_manager = ChatTailerManager()
