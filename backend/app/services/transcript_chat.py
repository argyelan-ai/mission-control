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
   "model":str|None,"effort":str|None,"contextWindow":int|None,
   "components":{"input":int,"cacheRead":int,"cacheCreation":int,"output":int}}

``inputTokens`` is deliberately the SUM of the three input-side fields
(``input`` + ``cacheRead`` + ``cacheCreation``); ``components`` carries the same
numbers unsummed for the context breakdown view. ``_stamp_usage_source``
replaces ``components`` with the CLI statusline's ``current_usage`` when that is
fresh — the transcript line describes one turn, the statusline describes the
whole live context window, so it is the better answer to "where did the window
go" whenever it exists.

`parse_transcript_line` also emits an internal ``_tool_result`` event for
``tool_result`` content blocks (type=="user" lines) — ``{"kind":"_tool_result",
"tool_use_id":str,"content":Any,"is_error":bool}``. ``read_history`` merges
these onto their matching ``tool`` event by ``tool_use_id`` == ``toolUseId``
(needed to disambiguate parallel tool calls within one assistant turn);
they never reach the frontend on their own.

``message.content`` has TWO shapes in real transcripts: the API's list-of-
blocks form, and a plain string — real interactively-typed user turns write
the latter (fix round 5, live-gate finding: string content silently produced
zero events, symptom "I don't see my own message"). Both ``_parse_user_entry``
and ``_parse_assistant_entry`` normalize a string ``content`` into a single
``{"type":"text","text":...}`` block up front so the rest of the block-loop
logic (slash-command rule included, for user entries) runs unchanged.
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

from app.config import settings
from app.services import sse
from app.services.pane_state import capture_pane, parse_pane_state, process_alive
from app.redis_client import RedisKeys
from app.services.token_harvester import _host_home, _should_attribute_boss_path

logger = logging.getLogger("mc.transcript_chat")

_DETAIL_TRUNCATE_LEN = 2000
_TITLE_MAX_LEN = 80

# Session-scan limits (find_active_session / transcript_allowed)
_LIVE_WINDOW_SECONDS = 60
_BOSS_SCAN_LINES = 20

# resolve_aliveness's transcript-age fallback (host/boss agents, or a docker
# agent whose process_alive check itself came back unknown): a transcript
# this recent is plausibly still an ongoing session, one this stale is not.
_ALIVENESS_IDLE_MAX_AGE_SECONDS = 12 * 3600  # 12 hours

# Host-runtime slugs that resolve to the Boss's own ~/.claude session dir —
# every other host agent (Hermes, Jarvis) has no transcript at all.
_BOSS_SLUGS = ("boss", "boss-host")

# Tools whose title is built from a file_path basename, prefixed "Read".
_FILE_PATH_READ_TOOLS = {"Read", "NotebookEdit"}


def resolve_context_window(model: str | None) -> int | None:
    """Resolves a model name to its context window size (tokens) via
    ``settings.context_windows``, so the frontend needs no hardcoded model
    map. Shared by both consumers of ``parse_transcript_line`` — ``read_history``
    and the live tailer — since both funnel through this one call site in
    ``_parse_assistant_entry``.

    Matching order:
    1. Exact match against a configured key.
    2. The LONGEST configured key that is a prefix of ``model`` (handles
       dated/versioned model strings, e.g. a future
       "claude-sonnet-4-6-20261201" against the configured "claude-sonnet-4-6").
    3. ``model`` contains the literal substring ``"[1m]"`` (Anthropic's 1M-
       context beta suffix) -> 1,000,000.
    4. Otherwise ``None`` — an unknown model gets no number rather than a
       guessed one.
    """
    if not model:
        return None

    windows = settings.context_windows
    if model in windows:
        return windows[model]

    prefix_matches = [key for key in windows if model.startswith(key)]
    if prefix_matches:
        return windows[max(prefix_matches, key=len)]

    if "[1m]" in model:
        return 1_000_000

    return None


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
    if isinstance(content, str):
        # Real interactive user turns write ``message.content`` as a plain
        # string, not the list-of-blocks shape tool-driven turns use —
        # verified live (fix round 5): {"type":"user","message":{"content":
        # "..."}}. Normalize to the one-text-block shape so the existing
        # block loop below (slash-command rule included) handles it unchanged.
        content = [{"type": "text", "text": content}]
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
    if isinstance(content, str):
        # Assistant entries are API-shaped (block arrays) in every real
        # transcript observed so far — but the user-entry string-content
        # discovery (fix round 5, see above) means the format isn't as
        # rigid as assumed. Tolerate it defensively the same way rather
        # than silently drop a whole assistant turn (including its usage
        # event) if it ever shows up here too.
        content = [{"type": "text", "text": content}]
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
        # `inputTokens` stays the SUM of the three input-side fields — every
        # existing consumer (the context ring's fallback estimate) depends on
        # that. `components` keeps them apart as well, so a breakdown view can
        # show where the window actually went without re-deriving anything.
        components = {
            "input": usage.get("input_tokens") or 0,
            "cacheRead": usage.get("cache_read_input_tokens") or 0,
            "cacheCreation": usage.get("cache_creation_input_tokens") or 0,
            "output": usage.get("output_tokens") or 0,
        }
        input_tokens = (
            components["input"] + components["cacheRead"] + components["cacheCreation"]
        )
        events.append(
            {
                "kind": "usage",
                "uuid": msg_uuid,
                "ts": ts,
                "inputTokens": input_tokens,
                "outputTokens": components["output"],
                "model": model,
                "effort": d.get("effort"),
                "contextWindow": resolve_context_window(model),
                "components": components,
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


async def resolve_aliveness(agent, session_path: Path) -> str:
    """Classifies a session's liveness for the history/tailer meta —
    ``"active" | "idle" | "ended"``. Fixes the old live-only semantics
    (mtime<60s == the ONLY signal, so an idle-but-still-running CLI read as
    "beendet"/ended everywhere — an operator-visible bug, since a session
    with nothing new to say for a few minutes is completely normal, not
    dead). ``live`` stays on the history/find_active_session meta unchanged
    for backward compat (== ``aliveness == "active"``); this is the new,
    richer signal alongside it.

    Priority order:
    1. Written within ``_LIVE_WINDOW_SECONDS`` -> ``"active"``.
    2. A NEWER session file now exists in the same directory (this one was
       rolled over / superseded) -> ``"ended"``. Checked via
       ``find_active_session`` again — cheap (one directory glob).
    3. ``pane_state.process_alive`` (docker cli-bridge, cached ~30s): the
       CLI process is confirmed running -> ``"idle"``; confirmed gone ->
       ``"ended"``.
    4. Otherwise (Boss/host — no process channel at all — or the docker
       check itself came back unknown): a transcript-age fallback. Within
       ``_ALIVENESS_IDLE_MAX_AGE_SECONDS`` -> ``"idle"``; older -> ``"ended"``.
    """
    try:
        mtime = session_path.stat().st_mtime
    except OSError:
        mtime = None

    if mtime is not None and (time.time() - mtime) < _LIVE_WINDOW_SECONDS:
        return "active"

    try:
        active = await asyncio.to_thread(find_active_session, session_path.parent)
    except OSError:
        active = None
    if active is not None and active[0] != session_path:
        return "ended"

    alive = await process_alive(agent)
    if alive is True:
        return "idle"
    if alive is False:
        return "ended"

    if mtime is not None and (time.time() - mtime) < _ALIVENESS_IDLE_MAX_AGE_SECONDS:
        return "idle"
    return "ended"


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


# ── Statusline state (I/O — reads the CLI's own context-window truth) ───────
#
# Claude Code invokes settings.json's `statusLine` command on every prompt,
# piping it a JSON blob with the CLI's own live token accounting
# (context_window.used_percentage, context_window.current_usage.*).
# docker/shared/statusline-mc.sh (wired in via plugin_manager.render_agent_settings,
# claude-harness agents only) mirrors that blob to
# <claude-config>/statusline-state/<session_id>.json. Reading it back here
# gives the chat context meter ground truth instead of a guess from the
# static settings.context_windows model->size map (resolve_context_window
# above) — the estimate stays as the fallback for models/agents where no
# fresh statusline write exists yet (Boss, whose ~/.claude isn't managed by
# this codebase; agents that haven't sent a prompt since the feature shipped).

_STATUSLINE_FRESH_SECONDS = 120


def _claude_config_root(session_path: Path) -> Path:
    """Given a session's transcript file path, returns the claude-config (or
    Boss's ~/.claude) root two levels above its parent directory — the
    inverse of resolve_transcript_dir's own shape:
    ``<root>/projects/<encoded-cwd>/<session>.jsonl``, so
    ``session_path.parent`` is ``projects/<encoded-cwd>`` and
    ``session_path.parent.parent`` is ``<root>``... two levels up from the
    session's *parent* dir, i.e. three from the file itself."""
    return session_path.parent.parent.parent


def read_statusline_state(claude_config_root: Path, session_id: str) -> dict[str, Any] | None:
    """Reads ``<claude_config_root>/statusline-state/<session_id>.json`` —
    the file docker/shared/statusline-mc.sh writes on every Claude Code
    prompt for this session. Returns ``{"usedPct": float, "usedTokens": int,
    "contextWindowSize": int}`` (``usedTokens`` = the sum of the four
    ``current_usage`` fields Claude Code reports; ``contextWindowSize`` =
    ``context_window.context_window_size``, the CLI's own live context-window
    size — present even before the session's first turn, so it's ground
    truth rather than the ``settings.context_windows`` model->size guess)
    when the file exists, was written less than ``_STATUSLINE_FRESH_SECONDS``
    ago (older means no CLI turn has run recently enough to trust it — the
    agent may have switched sessions or the script may be broken), and
    parses as the expected shape. ``None`` on any failure — missing file
    (most agents, always for Boss), stale mtime, or malformed JSON. Never
    raises; the caller falls back to the static ``resolve_context_window``
    estimate."""
    state_file = claude_config_root / "statusline-state" / f"{session_id}.json"
    try:
        mtime = state_file.stat().st_mtime
    except OSError:
        return None
    if (time.time() - mtime) >= _STATUSLINE_FRESH_SECONDS:
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        ctx = data["context_window"]
        usage = ctx["current_usage"]
        used_pct = float(ctx["used_percentage"])
        context_window_size = int(ctx["context_window_size"])
        components = {
            "input": int(usage.get("input_tokens") or 0),
            "cacheRead": int(usage.get("cache_read_input_tokens") or 0),
            "cacheCreation": int(usage.get("cache_creation_input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        }
        used_tokens = sum(components.values())
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "usedPct": used_pct,
        "usedTokens": used_tokens,
        "contextWindowSize": context_window_size,
        "components": components,
    }


def _stamp_usage_source(ev: dict[str, Any], claude_config_root: Path, session_id: str) -> None:
    """Mutates a ``usage`` event in place with ``usedPct``/``source``,
    preferring fresh statusline state (source ``"cli"``) over the static
    ``contextWindow`` estimate parse_transcript_line already stamped
    (source ``"estimate"``, ``usedPct`` left ``None`` for the frontend to
    compute from ``contextWindow`` itself). When statusline state is fresh,
    its own ``context_window_size`` also OVERRIDES the ``contextWindow``
    estimate — the CLI reports its actual context window directly, no need
    to guess from the model name."""
    state = read_statusline_state(claude_config_root, session_id)
    if state is not None:
        ev["usedPct"] = state["usedPct"]
        ev["source"] = "cli"
        ev["contextWindow"] = state["contextWindowSize"]
        # The CLI's own per-field usage describes the WHOLE live context, while
        # the transcript line only describes that one turn — so when it is
        # available it wins, the same way usedPct/contextWindow do. Otherwise
        # the turn-level breakdown parse_transcript_line stamped stays.
        ev["components"] = state["components"]
    else:
        ev["usedPct"] = None
        ev["source"] = "estimate"


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
    claude_config_root = _claude_config_root(path)
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
                    elif ev["kind"] == "usage":
                        _stamp_usage_source(ev, claude_config_root, session_id)

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

    # Pane-state probe cadence: every 2nd tick (~every other POLL_INTERVAL)
    # rather than every tick — capture-pane is a docker exec round-trip, not
    # worth paying on every 1s poll.
    STATE_PROBE_EVERY_N_TICKS = 2

    # A transcript that hasn't grown in this long reads as idle for the
    # Boss/host fallback (no pane to capture) and as the transcript_active
    # signal parse_pane_state uses to disambiguate an input-prompt marker
    # (still typing vs. waiting).
    STATE_ACTIVE_WINDOW_SECONDS = 20

    def __init__(self) -> None:
        self._refcounts: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def acquire(self, agent_id: str, path: Path, agent: Any | None = None) -> None:
        """Registers one more client for ``agent_id``. Starts the poll task
        if this is the first client; otherwise just bumps the refcount — the
        already-running task keeps tailing from wherever it is.

        ``agent`` (optional) is threaded through to the pane-state probe —
        it's the only place in the tailer that needs the Agent row itself
        (for ``capture_pane``'s runtime/slug lookup) rather than just a path.
        None disables the probe entirely (no state events published), which
        is also how the raw ``ChatTailerManager`` unit tests exercise the
        tailer without a DB-backed Agent.

        The starting offset is stat()'d HERE, synchronously, rather than as
        the first line of ``_run`` — a freshly created task doesn't actually
        start executing until the event loop regains control (the caller's
        next ``await``), so if the size were read inside the task body, a
        caller that writes to the file right after ``acquire()`` returns
        (with no intervening await) would race it: the task would only get
        scheduled after that write and seed from the wrong (post-write)
        size. Reading it here, before the task is even created, closes that
        window."""
        count = self._refcounts.get(agent_id, 0)
        self._refcounts[agent_id] = count + 1
        if count == 0:
            try:
                initial_offset = path.stat().st_size
            except OSError:
                initial_offset = 0
            self._tasks[agent_id] = asyncio.create_task(
                self._run(agent_id, path, initial_offset, agent)
            )

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

    async def _run(
        self,
        agent_id: str,
        initial_path: Path,
        initial_offset: int,
        agent: Any | None = None,
    ) -> None:
        channel = RedisKeys.agent_chat_channel(agent_id)
        tdir = initial_path.parent
        current_path = initial_path
        tick = 0
        last_pane_state: dict[str, Any] | None = None
        # Seeded from the file's size AT ACQUIRE TIME — NOT 0 — so a first
        # connect (or a refcount 0->1 cycle) tails only new lines instead of
        # re-reading and re-broadcasting the entire existing transcript as
        # live events (which would duplicate everything /chat/history
        # already returned). See acquire()'s docstring for why this is
        # computed there and passed in rather than stat'd here.
        offset = initial_offset
        buffer = b""
        tool_events_by_id: dict[str, dict[str, Any]] = {}
        # Cleared on session rollover (below) — bounded by one session's
        # lifetime, not by an explicit cap.
        seen_uuids: set[str] = set()
        # Dedup guard for the rejected-rollover warning below — without it a
        # disallowed newest-mtime file that keeps existing (e.g. Mark's own
        # personal session sitting in Boss's transcript dir) would log once
        # per poll tick forever.
        rejected_rollover_path: Path | None = None

        try:
            while True:
                await asyncio.sleep(self.POLL_INTERVAL)
                tick += 1

                try:
                    if agent is not None and tick % self.STATE_PROBE_EVERY_N_TICKS == 0:
                        new_state = await self._compute_pane_state(agent, current_path)
                        if new_state != last_pane_state:
                            last_pane_state = new_state
                            await sse.broadcast(
                                channel, "chat_event", {"kind": "state", **new_state}
                            )

                    try:
                        active = await asyncio.to_thread(find_active_session, tdir)
                    except OSError:
                        active = None
                    if active is not None and active[0] != current_path:
                        # Re-run the same Boss privacy gate the SSE handshake
                        # enforces at connect time (agent_chat.py:80) — a
                        # rollover mid-stream is a second, later "which file
                        # may this agent publish" decision and must not
                        # bypass it (review finding I-1). ``agent`` is only
                        # ``None`` in tests that don't exercise the gate; the
                        # real caller (acquire(), agent_chat.py:121) always
                        # passes it. ``transcript_allowed`` does blocking file
                        # I/O (opens the file, scans up to 20 lines) and this
                        # branch re-runs it every tick for as long as the
                        # rejected file stays newest — to_thread it, same
                        # rule as every other disk read in this loop.
                        allowed = True
                        if agent is not None:
                            allowed = await asyncio.to_thread(
                                transcript_allowed, agent, active[0]
                            )
                        if not allowed:
                            if active[0] != rejected_rollover_path:
                                rejected_rollover_path = active[0]
                                logger.warning(
                                    "chat tailer: rollover to %s rejected by "
                                    "transcript_allowed (agent_id=%s) — keeping "
                                    "current session %s",
                                    active[0], agent_id, current_path,
                                )
                            # Do NOT switch — keep tailing current_path below.
                        else:
                            rejected_rollover_path = None
                            current_path = active[0]
                            offset = 0
                            buffer = b""
                            tool_events_by_id = {}
                            seen_uuids = set()
                            last_pane_state = None
                            # aliveness is hardcoded "active" here rather than
                            # a fresh resolve_aliveness() call: a rollover
                            # only fires for a file find_active_session JUST
                            # confirmed is the newest, freshly written — by
                            # construction its mtime is within the live
                            # window, so an extra async round trip would only
                            # re-derive what's already known for free.
                            await sse.broadcast(
                                channel, "chat_event",
                                {"kind": "session_changed", "aliveness": "active"},
                            )
                            continue

                    try:
                        new_offset, chunk = await asyncio.to_thread(
                            _read_new_chunk, current_path, offset
                        )
                    except OSError:
                        # File disappeared (rotated/deleted mid-session) —
                        # state is unknown, but the directory keeps getting
                        # polled so a replacement (or the same path
                        # reappearing) is picked up.
                        continue

                    if chunk is None:
                        continue

                    offset = new_offset
                    buffer += chunk
                    lines = buffer.split(b"\n")
                    buffer = lines.pop()  # last element: partial line (or b"") — held for next tick

                    for raw_bytes in lines:
                        raw_line = raw_bytes.decode("utf-8", errors="replace").strip()
                        if not raw_line:
                            continue

                        entry_uuid = _peek_uuid(raw_line)
                        if entry_uuid is not None:
                            if entry_uuid in seen_uuids:
                                continue
                            seen_uuids.add(entry_uuid)

                        for ev in parse_transcript_line(raw_line):
                            if ev["kind"] == "_tool_result":
                                tool_ev = tool_events_by_id.pop(ev.get("tool_use_id"), None)
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
                            elif ev["kind"] == "usage":
                                # Sync file I/O (stat + read of the statusline
                                # state file) -> to_thread, same rule as
                                # _read_new_chunk above: the event loop never
                                # blocks on disk. current_path is re-derived
                                # per event (not hoisted) since a rollover mid-
                                # tick swaps it.
                                await asyncio.to_thread(
                                    _stamp_usage_source,
                                    ev,
                                    _claude_config_root(current_path),
                                    current_path.stem,
                                )

                            await sse.broadcast(channel, "chat_event", ev)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A single bad iteration (e.g. a transient Redis error in
                    # sse.broadcast) must not silently kill the task while
                    # clients stay connected — log and keep tailing.
                    logger.error(
                        "chat tailer: poll iteration failed for agent %s", agent_id, exc_info=True
                    )
        finally:
            # A silently dead tailer looks identical to "no new events yet"
            # from the outside — make every exit visible.
            logger.warning("chat tailer loop exited (agent_id=%s)", agent_id)

    async def _compute_pane_state(self, agent: Any, current_path: Path) -> dict[str, Any]:
        """One probe tick's worth of state classification (A6). Computes
        ``transcript_active`` from the current session file's mtime (used
        both as ``parse_pane_state``'s disambiguation signal and as the
        entire signal for agents ``capture_pane`` can't reach), then either
        parses a captured pane snapshot or falls back to the mtime-only
        heuristic for Boss/host agents — which, per the design brief, must
        never report ``permission_prompt`` since there's no pane text to
        have found one in. Also stamps ``aliveness`` (``resolve_aliveness``)
        into the result — cheap to add here since it rides the same already-
        throttled probe tick (``STATE_PROBE_EVERY_N_TICKS``) rather than
        polling on its own cadence, and its own docker-side check
        (``pane_state.process_alive``) is itself cached ~30s."""
        try:
            mtime = await asyncio.to_thread(lambda: current_path.stat().st_mtime)
            transcript_active = (time.time() - mtime) < self.STATE_ACTIVE_WINDOW_SECONDS
        except OSError:
            transcript_active = False

        aliveness = await resolve_aliveness(agent, current_path)

        pane_text = await capture_pane(agent)
        if pane_text is None:
            return {
                "status": "working" if transcript_active else "idle",
                "prompt": None,
                "aliveness": aliveness,
            }

        return {**parse_pane_state(pane_text, transcript_active), "aliveness": aliveness}


def _read_new_chunk(path: Path, offset: int) -> tuple[int, bytes | None]:
    """Blocking: stat + binary-read the bytes appended since ``offset``.
    Runs via ``asyncio.to_thread`` — never call directly from the event
    loop. Binary mode + byte offsets (not text mode) so a multi-byte UTF-8
    character split across two polls can't be double-counted or truncated;
    the caller decodes only after buffering complete lines.

    Returns ``(new_offset, chunk)``, or ``(offset, None)`` if there's
    nothing new to read (also on a read failure after the stat succeeded).
    """
    size = path.stat().st_size
    if size <= offset:
        return offset, None
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    return size, chunk


def _peek_uuid(line: str) -> str | None:
    """Best-effort extraction of the top-level ``uuid`` field for live-path
    dedup, mirroring ``read_history``'s ``seen_uuids`` — Claude Code can
    repeat a line verbatim across a resumed session. Never raises."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return d.get("uuid")


tailer_manager = ChatTailerManager()
