"""A task's whole work for the agent chat — read-only (spec A, PR-2).

Variant A shows the steps of a task INSIDE the agent chat, at their time:
every thought as one chapter line, the tools under it, the texts, the
comments. This module answers the three reads the chat needs:

* :func:`task_stubs` — which tasks belong into this chat window: every LIVE
  task assigned to the agent (no limit), plus the newest finished ones as
  cards (one stub row, no file read), each with its outcome line.
* :func:`task_steps` — one task's steps across ALL its attempts, slim (one
  short row per step: a thought's first sentence, a tool's title, never a
  tool's output), with a cursor so a poll only carries new rows, and an
  ETag so an unchanged poll is a 304.
* :func:`step_detail` — one step in full, on tap (a thought's text, a tool's
  output, the task text, a live thought's end).

Harness-neutral: files come from the adapter (``find_task_sessions``,
``transcript_allowed``, ``preview_channel``) and are parsed by the adapter's
own parser via ``read_history`` — the same normalized events the chat
renders. A harness whose tasks run inside the chat answers
``{"applies": false}`` (same gate as ``chat_task_activity``).

Safety (the rows are read out to the chat, so a wrong file is a leak):

* ``transcript_allowed`` per FILE, fail closed;
* every file is opened with ``O_NOFOLLOW`` and its ``fstat`` identity must
  equal the ``lstat`` taken just before — a file swapped for a symlink is
  skipped (pattern of ``services/heads/transcript.py``);
* a path never comes from the URL: row ids are ``<attempt>:<index>``,
  checked by a regex, and looked up in the discovered list.

Cost: slim rows are cached per file identity (an unchanged file is never
parsed twice), discovery per task for a few seconds.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import shlex
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.deliverable import TaskDeliverable
from app.models.task import Task, TaskComment, TaskEvent
from app.services.chat_run_context import review_run_holds
from app.services.transcript_adapters import adapter_for
from app.services.transcript_chat import preview_event, read_history

logger = logging.getLogger(__name__)

NOT_APPLICABLE: dict[str, Any] = {"applies": False}

#: A task is LIVE in a chat only while it is assigned to that agent and in
#: one of these states (or delivered: ``inbox`` with ``dispatched_at``).
LIVE_STATUSES = ("in_progress", "waiting", "blocked", "user_test")
#: The one step into ``in_progress`` that starts a new run without an inbox
#: step: a done phase sent back for a rewrite (``task_lifecycle``).
RUN_START_REWRITE_REASON = "phase_rewrite_request"
#: Defensive cap of the live query (normally 0–1 live tasks per agent).
_LIVE_CAP = 10
#: Task session files written this long before the task was created are not its.
_SINCE_SLACK = timedelta(minutes=2)
#: A preview without change for this long is ``stale`` (N2).
_STALE_S = 600.0
#: Bytes read from the END of a preview file (it can be tens of MB).
_PREVIEW_TAIL_BYTES = 512 * 1024
#: Discovery cache (a sessions tree has hundreds of files).
_DISCOVERY_TTL_S = 10.0
_DISCOVERY_EMPTY_TTL_S = 2.0
#: Slim rows of this many files are kept (by file identity).
_PARSE_CACHE_MAX = 64
#: ``read_history`` page size that never cuts: row ids are event indexes and
#: must stay stable while a file grows.
_ALL_EVENTS = 10**9

#: Caps of the slim rows (spec A §4.2).
_TITLE_MAX = 120
_COMMENT_MAX = 600
_TEXT_MAX = 1500
_DELIVERY_LEAD_MAX = 120
_SYSTEM_MAX = 200
_OUTCOME_MAX = 200
_LIVE_TEXT_MAX = 600
#: Caps of the full row (detail).
_DETAIL_TEXT_MAX = 20_000
_DETAIL_LIVE_MAX = 2_000

ROW_ID_RE = re.compile(r"^\d{1,2}:\d{1,6}(?:\.\d{1,3})?$")
_CURSOR_RE = re.compile(r"^v1(?:\.\d{1,9})*$")
MAX_OPEN_IDS = 50


def _now() -> float:
    """Wall clock (one place, so tests can move it)."""
    return time.time()


# ── pure helpers ────────────────────────────────────────────────────────────

_LEAD_MIN = 24
_LEAD_MAX = 140
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_LEADING_MARKERS = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+)+")


def _clean_part(part: str) -> str:
    return " ".join(_LEADING_MARKERS.sub("", part).split())


def lead_of(text: str | None) -> tuple[str, bool]:
    """The first sentence(s) of a thought as its chapter line, and whether
    more text follows. Sentences are taken until the line says something
    (≥ 24 chars and not ending with ``:``); markdown markers, ``**`` and
    backticks go, whitespace collapses; a line over 140 chars is cut at the
    last space with ``…``."""
    plain = (text or "").replace("**", "").replace("`", "")
    parts = [p for p in (_clean_part(x) for x in _SENTENCE_SPLIT.split(plain)) if p]
    if not parts:
        return "", False
    out = ""
    used = 0
    for part in parts:
        out = f"{out} {part}" if out else part
        used += 1
        if len(out) >= _LEAD_MIN and not out.endswith(":"):
            break
    more = used < len(parts)
    if len(out) > _LEAD_MAX:
        cut = out[:_LEAD_MAX]
        space = cut.rfind(" ")
        out = (cut[:space] if space > _LEAD_MAX // 2 else cut).rstrip() + "…"
        more = True
    return out, more


_SHELL_PUNCT = "();<>|&\n"
_SEPARATOR_CHARS = frozenset(";&\n")
_COMMENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_UNLITERAL = ("$(", "${", "`")


def comment_of(event: dict[str, Any]) -> dict[str, str] | None:
    """``{"type", "text"}`` when a tool call is an ``mc comment <type>
    <text…>`` the agent wrote (alone or after ``cd … &&``), else ``None``.

    Only a LITERAL comment counts: a heredoc, a pipe, a redirect or a shell
    substitution means the shown text is not what the command posted —
    then the step stays a plain tool row. Broken quoting, too. (Tied to the
    ``bash`` tool name of omp/the bridge; moving it into the adapter is a
    follow-up.)"""
    if event.get("name") != "bash":
        return None
    detail = event.get("detail")
    command = detail.get("command") if isinstance(detail, dict) else None
    if not isinstance(command, str) or "mc" not in command or "comment" not in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCT)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _SEPARATOR_CHARS:
            segments.append([])
        else:
            segments[-1].append(token)
    for seg in segments:
        if len(seg) < 4 or seg[0] != "mc" or seg[1] != "comment":
            continue
        if not _COMMENT_TYPE_RE.match(seg[2]):
            return None
        rest = seg[3:]
        if any(t and set(t) <= set(_SHELL_PUNCT) for t in rest):
            return None  # a redirect, pipe or heredoc: not literal
        text = " ".join(rest).strip()
        if not text or any(m in text for m in _UNLITERAL):
            return None
        return {"type": seg[2], "text": text}
    return None


_GLUE_PREFIX = 200
#: The bridge stores a long thought as head + marker + tail
#: (``acp_chat_events._keep_thought``) — its END is still findable by the tail.
_CAP_HALF = 6000
_CAP_MARK = "\n…\n"


@dataclass(frozen=True)
class GluedSplit:
    """What a legacy glued final line held besides the thoughts.

    ``pieces``: ``(thought_position, text)`` — the text right after the
    thought at that position in the given thought list, or ``-1`` for the
    tail after the last thought. ``dropped``: thoughts that matched only by
    their start (their end is unknown, the text after them is lost)."""

    pieces: list[tuple[int, str]]
    dropped: int


def _thought_end(text: str, thought: str, pos: int) -> int | None:
    """Where ``thought`` (found at ``pos`` of ``text``) ends, or ``None``."""
    if text.startswith(thought, pos):
        return pos + len(thought)
    if (
        len(thought) == 2 * _CAP_HALF + len(_CAP_MARK)
        and thought[_CAP_HALF:_CAP_HALF + len(_CAP_MARK)] == _CAP_MARK
        and text.startswith(thought[:_CAP_HALF], pos)
    ):
        tail = thought[-_CAP_HALF:]
        found = text.find(tail, pos + _CAP_HALF)
        if found >= 0:
            return found + len(tail)
    return None


def split_glued(text: str, thoughts: list[str]) -> GluedSplit | None:
    """Split a task's legacy final assistant line (spec A M7/F8), or ``None``
    when ``text`` is a normal reply.

    Before the bridge fix (PR-1) the task path wrote ALL thought text plus
    the reply into the turn's one final line (up to 515,000 chars) — the
    thoughts are also stored on their own. Such a line is *glued* when it
    starts with the first thought (over that thought's real length, which
    can be short) and the starts of at least ``min(3, n)`` thoughts occur in
    it in order. It is then not shown; the reply text between the thoughts
    and after the last one is recovered as pieces."""
    indexed = [(j, t) for j, t in enumerate(thoughts) if t]
    if not indexed or not text or not text.startswith(indexed[0][1][:_GLUE_PREFIX]):
        return None
    need = min(3, len(indexed))
    hits = 0
    cursor = 0
    for _j, thought in indexed:
        pos = text.find(thought[:_GLUE_PREFIX], cursor)
        if pos < 0:
            continue
        hits += 1
        cursor = pos + 1
    if hits < need:
        return None

    pieces: list[tuple[int, str]] = []
    dropped = 0
    cursor = 0
    prev_end: int | None = None
    prev_j: int | None = None
    for j, thought in indexed:
        pos = text.find(thought[:_GLUE_PREFIX], cursor)
        if pos < 0:
            continue
        if prev_end is not None and prev_j is not None:
            gap = text[prev_end:pos].strip()
            if gap:
                pieces.append((prev_j, gap))
        end = _thought_end(text, thought, pos)
        if end is None:
            dropped += 1
            prev_end = None
            cursor = pos + 1
        else:
            prev_end = end
            cursor = end
        prev_j = j
    if prev_end is not None:
        tail = text[prev_end:].strip()
        if tail:
            pieces.append((-1, tail))
    return GluedSplit(pieces=pieces, dropped=dropped)


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _ellipsis(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plain_line(text: str | None, limit: int = _OUTCOME_MAX) -> str | None:
    """One plain line out of a markdown text (outcome lines, stub titles)."""
    if not text:
        return None
    lines = [_clean_part(x) for x in text.replace("**", "").replace("`", "").splitlines()]
    joined = " ".join(x for x in lines if x)
    return _ellipsis(joined, limit) if joined else None


_PERMISSION_REQUEST_RE = re.compile(r"^Freigabe-Anfrage \([^)]*\): (?P<what>.*?)(?: \[[^\]]*\])?$", re.S)
_PERMISSION_DECISION_RE = re.compile(r"^Freigabe entschieden: (?P<choice>\S+) — (?P<what>.*)$", re.S)


def _system_row(event: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """A system line: a permission request/decision (both sent — the client
    merges them, N8), else the line's first words."""
    text = str(event.get("text") or "")
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    title = source.get("title")
    if title == "acp-permission":
        m = _PERMISSION_REQUEST_RE.match(text)
        if m:
            return {**base, "k": "system", "what": _ellipsis(m.group("what"), _SYSTEM_MAX), "request": True}
    if title == "acp-permission-decision":
        m = _PERMISSION_DECISION_RE.match(text)
        if m:
            return {**base, "k": "system", "what": _ellipsis(m.group("what"), _SYSTEM_MAX),
                    "decision": m.group("choice")}
    row = {**base, "k": "system", "what": plain_line(text, _SYSTEM_MAX) or ""}
    kind = source.get("kind")
    if isinstance(kind, str) and kind and kind != "system":
        row["src"] = kind
    return row


def _tool_state(event: dict[str, Any]) -> str:
    if event.get("result") is None:
        return "running"
    return "error" if event.get("status") == "error" else "done"


def _is_delivery(event: dict[str, Any]) -> bool:
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    return event.get("role") == "teammate" and source.get("kind") in ("task", "nudge")


#: Teammate lines that are NOT input to the model: they appear in the middle
#: of a turn (a permission request/decision, a chat notice, an error card).
_IN_TURN_SOURCES = frozenset({"system", "notice", "error"})


def _starts_turn(event: dict[str, Any]) -> bool:
    """Does this event hand the model new input — a delivery, a nudge, an
    inbox message, a user line? Each such input starts a new turn, and each
    turn wrote its OWN legacy glued final line, starting with THAT turn's
    first thought (a continue-nudge runs a new turn in the same session,
    ``bridge.py`` ``acp_run(nudge, origin_kind="nudge")``)."""
    if event.get("kind") != "message":
        return False
    role = event.get("role")
    if role == "user":
        return True
    if role != "teammate":
        return False
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    return source.get("kind") not in _IN_TURN_SOURCES


def slim_events(events: list[dict[str, Any]], attempt: int) -> list[tuple[int, dict[str, Any]]]:
    """One attempt's events -> slim rows ``(event_index, row)``, in event
    order. Row id = ``"<attempt>:<index>"``; pieces recovered from a legacy
    glued final line get ``"<attempt>:<index>.<k>"`` and the ts of the
    thought they followed (the tail: the line's own ts) — the chat sorts by
    ts, so they land right after their thought. The thoughts a glued line
    is matched against are those of its own turn (:func:`_starts_turn`)."""
    rows: list[tuple[int, dict[str, Any]]] = []
    thoughts: list[tuple[int, dict[str, Any]]] = []
    delivered = False
    for i, ev in enumerate(events):
        kind = ev.get("kind")
        base = {"id": f"{attempt}:{i}", "ts": ev.get("ts")}
        if _starts_turn(ev):
            thoughts = []  # a glued line holds the thoughts of its own turn only
        if kind == "thinking":
            text = ev.get("text") or ""
            if not text.strip():
                continue
            thoughts.append((i, ev))
            lead, more = lead_of(text)
            row: dict[str, Any] = {**base, "k": "think", "lead": lead}
            if more:
                row["more"] = True
            if isinstance(ev.get("durationMs"), int):
                row["ms"] = ev["durationMs"]
            rows.append((i, row))
        elif kind == "tool":
            comment = comment_of(ev)
            if comment is not None:
                text, more = _cap(comment["text"], _COMMENT_MAX)
                row = {**base, "k": "comment", "type": comment["type"], "text": text}
                if more:
                    row["more"] = True
            else:
                row = {**base, "k": "tool", "name": ev.get("name"),
                       "title": _ellipsis(str(ev.get("title") or ev.get("name") or ""), _TITLE_MAX),
                       "st": _tool_state(ev)}
                if ev.get("stats"):
                    row["stats"] = ev["stats"]
            rows.append((i, row))
        elif kind == "message":
            role = ev.get("role")
            if role == "assistant":
                text = ev.get("text") or ""
                split = split_glued(text, [t.get("text") or "" for _, t in thoughts])
                if split is not None:
                    if split.dropped:
                        logger.info("chat_task_steps: glued line %s: %d piece(s) after capped thoughts dropped",
                                    base["id"], split.dropped)
                    for k, (pos, piece) in enumerate(split.pieces):
                        ts = thoughts[pos][1].get("ts") if pos >= 0 else ev.get("ts")
                        capped, more = _cap(piece, _TEXT_MAX)
                        row = {"id": f"{base['id']}.{k}", "ts": ts, "k": "text", "text": capped}
                        if more:
                            row["more"] = True
                        rows.append((i, row))
                    continue
                if not text.strip():
                    continue
                capped, more = _cap(text, _TEXT_MAX)
                row = {**base, "k": "text", "text": capped}
                if more:
                    row["more"] = True
                rows.append((i, row))
            elif _is_delivery(ev) and not delivered:
                delivered = True
                if attempt == 1:
                    source = ev.get("source") or {}
                    first = next((ln for ln in (ev.get("text") or "").splitlines() if ln.strip()), "")
                    lead = plain_line(source.get("title") or first, _DELIVERY_LEAD_MAX) or ""
                    rows.append((i, {**base, "k": "delivery", "lead": lead, "more": True}))
                else:
                    rows.append((i, {**base, "k": "attempt", "n": attempt}))
            else:
                rows.append((i, _system_row(ev, base)))
        # usage, elicitation and other kinds: no row
    return rows


def parse_cursor(cursor: str | None) -> list[int] | None:
    if not isinstance(cursor, str) or not _CURSOR_RE.match(cursor):
        return None
    return [int(x) for x in cursor.split(".")[1:]]


def format_cursor(counts: list[int]) -> str:
    return ".".join(["v1", *(str(c) for c in counts)])


def page_rows(
    attempts: list[list[tuple[int, dict[str, Any]]]],
    counts: list[int],
    after: str | None,
    limit: int,
) -> dict[str, Any]:
    """One page of rows over all attempts (spec A §4.2, S11).

    ``after`` = the cursor the client has (events per attempt it has seen).
    Without one: the newest ``limit`` rows, ``omitted`` = the older ones.
    With one: the rows behind it, OLDEST first, at most ``limit``; the new
    cursor moves only to the last row delivered, ``hasMore`` says to fetch
    again at once — a capped delta never leaves a gap. An unparsable
    cursor, one with more attempts than exist or a count beyond a file
    -> ``reset`` + the newest rows."""
    cur = parse_cursor(after) if after is not None else None
    reset = after is not None and (
        cur is None or len(cur) > len(counts) or any(c > counts[a] for a, c in enumerate(cur))
    )
    if after is None or reset:
        flat = [row for rows in attempts for _i, row in rows]
        page = flat[-limit:] if limit else []
        return {"rows": page, "cursor": format_cursor(counts), "hasMore": False,
                "reset": reset, "omitted": len(flat) - len(page)}

    assert cur is not None
    cur = cur + [0] * (len(counts) - len(cur))
    out: list[dict[str, Any]] = []
    new_cur = list(cur)
    has_more = False
    for a, rows in enumerate(attempts):
        pending = [(i, row) for i, row in rows if i >= cur[a]]
        room = limit - len(out)
        if len(pending) <= room:
            out.extend(row for _i, row in pending)
            new_cur[a] = counts[a]
            continue
        take = pending[:room]
        cut_index = pending[room][0]
        # Pieces of one event travel together: never stop in the middle.
        if take and take[-1][0] == cut_index:
            kept = [(i, row) for i, row in take if i != cut_index]
            if kept or out:
                take = kept
            else:  # one event alone is bigger than the page: send it whole
                take = [(i, row) for i, row in pending if i == cut_index]
        out.extend(row for _i, row in take)
        new_cur[a] = take[-1][0] + 1 if take else cur[a]
        has_more = True
        break
    return {"rows": out, "cursor": format_cursor(new_cur), "hasMore": has_more,
            "reset": False, "omitted": 0}


# ── files ───────────────────────────────────────────────────────────────────

_discovery: dict[tuple[str, str], tuple[float, list[Any]]] = {}
_parsed: OrderedDict[tuple[Any, ...], "_Attempt"] = OrderedDict()
#: Reads run in worker threads (``asyncio.to_thread``): one lock for both caches.
_cache_lock = threading.Lock()


def clear_caches() -> None:
    with _cache_lock:
        _discovery.clear()
        _parsed.clear()


@dataclass
class _Attempt:
    rows: list[tuple[int, dict[str, Any]]]
    count: int
    started_at: str | None
    newest_ts: datetime | None


@dataclass
class _Opened:
    path: Path
    st: os.stat_result
    fh: Any

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass


def open_pinned(
    root: Path, path: Path, identity: tuple[int, int] | None = None, *, binary: bool = False
) -> _Opened | None:
    """Open ``path`` (inside ``root``) read-only, or ``None`` — the caller
    then skips the file, never falls back to a plain ``open()``.

    Spec A S3, two locks:

    * **No symlink anywhere below ``root``.** The path is walked one part at
      a time with ``O_NOFOLLOW`` relative to the folder opened before it
      (``dir_fd``). ``O_NOFOLLOW`` alone guards only the LAST part — an
      agent's sandbox could swap ``.mc/`` or its cwd folder for a symlink
      after the path was checked, and a plain open would go through it. A
      path that is not lexically inside ``root`` is refused.
    * **The file is the one discovery saw.** With ``identity`` (the
      ``(st_dev, st_ino)`` of discovery's ``lstat``, :class:`TaskFile`) the
      opened file's ``fstat`` must match it. It is never compared with a
      fresh ``lstat``: that would run after a swap too and agree with it.

    ``O_NONBLOCK`` on the last open: a FIFO swapped in can never hang the
    worker thread (it is then refused as "not a regular file")."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    fd = -1
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (identity is not None and (st.st_dev, st.st_ino) != identity):
            os.close(fd)
            return None
        os.set_blocking(fd, True)
        fh = os.fdopen(fd, "rb") if binary else os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return _Opened(path=path, st=st, fh=fh)


def _discover(adapter: Any, tdir: Path, task_id: str, since: float | None) -> list[Any]:
    """The task's files with their discovery identity (:class:`TaskFile`),
    cached briefly — the identity stays pinned to the FIRST lstat until the
    entry is dropped (:func:`_forget`) or expires."""
    key = (str(tdir), task_id)
    with _cache_lock:
        hit = _discovery.get(key)
    now = time.monotonic()
    if hit is not None:
        ttl = _DISCOVERY_TTL_S if hit[1] else _DISCOVERY_EMPTY_TTL_S
        if now - hit[0] < ttl and all(os.path.lexists(f.path) for f in hit[1]):
            return hit[1]
    files = list(adapter.find_task_sessions(tdir, task_id, since))
    with _cache_lock:
        if len(_discovery) > 256:
            _discovery.clear()
        _discovery[key] = (now, files)
    return files


def _forget(tdir: Path, task_id: str) -> None:
    """Drop a task's discovery: the next poll discovers (and pins) again."""
    with _cache_lock:
        _discovery.pop((str(tdir), task_id), None)


def _quiet(adapter: Any) -> Any:
    """The adapter without side lookups: no usage stamping from foreign
    config paths, no subagent folder scan."""
    return dataclasses.replace(adapter, stamp_usage=lambda ev, p: None, subagent_runs=lambda p: [])


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_events(opened: _Opened, adapter: Any) -> tuple[list[dict[str, Any]], str | None]:
    history = read_history(opened.path, _quiet(adapter), limit=_ALL_EVENTS, fileobj=opened.fh)
    return history.get("events") or [], (history.get("session") or {}).get("startedAt")


def _attempt_rows(opened: _Opened, adapter: Any, n: int) -> _Attempt:
    st = opened.st
    key = (adapter.name, str(opened.path), n, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    with _cache_lock:
        hit = _parsed.get(key)
        if hit is not None:
            _parsed.move_to_end(key)
    if hit is not None:
        opened.close()
        return hit
    events, started = _read_events(opened, adapter)
    newest = max((t for t in (_parse_ts(e.get("ts")) for e in events) if t), default=None)
    attempt = _Attempt(rows=slim_events(events, n), count=len(events), started_at=started, newest_ts=newest)
    with _cache_lock:
        _parsed[key] = attempt
        while len(_parsed) > _PARSE_CACHE_MAX:
            _parsed.popitem(last=False)
    return attempt


def _last_preview(opened: _Opened) -> dict[str, Any] | None:
    """The newest ``acp-preview`` snapshot of an (opened) preview file, read
    from its END only. Same reading as ``chat_task_activity.last_preview``,
    on a file opened with ``O_NOFOLLOW``."""
    try:
        with opened.fh as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - _PREVIEW_TAIL_BYTES))
            chunk = fh.read()
    except OSError:
        return None
    for raw in reversed(chunk.split(b"\n")):
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("customType") != "acp-preview":
            continue
        content, ts = d.get("content"), d.get("timestamp")
        if not isinstance(content, str) or not content or not isinstance(ts, str):
            return None
        return preview_event(content, ts, d.get("data"))
    return None


def _open_preview(agent: Any, adapter: Any, tdir: Path, last: Path) -> _Opened | None:
    if adapter.preview_channel is None:
        return None
    ppath = adapter.preview_channel(last)
    if ppath is None:
        return None
    try:
        if not ppath.resolve().is_relative_to(tdir.resolve()):
            return None
    except OSError:
        return None
    if not adapter.transcript_allowed(agent, ppath):
        return None
    # No discovery identity exists for the preview (``preview_channel``
    # picks the newest file of ``previews/`` on each call); the walk below
    # ``tdir`` without any symlink is what keeps it inside the agent folder.
    return open_pinned(tdir, ppath, binary=True)


def _live_row(preview: _Opened, newest: datetime | None, text_max: int) -> dict[str, Any] | None:
    stale = _now() - preview.st.st_mtime > _STALE_S
    ev = _last_preview(preview)
    if ev is None:
        return None
    p_ts = _parse_ts(ev.get("ts"))
    # A snapshot the transcript already overtook is not live any more (same
    # rule as the chat's own seed and ``chat_task_activity``).
    if p_ts is None or (newest is not None and p_ts <= newest):
        return None
    if ev.get("block") == "thinking":
        row: dict[str, Any] = {"block": "thinking", "startedAt": ev.get("startedAt"), "ts": ev.get("ts"),
                               "chars": ev.get("chars")}
        if text_max > _LIVE_TEXT_MAX:
            row["text"] = (ev.get("text") or "")[-text_max:]
    else:
        row = {"block": "text", "ts": ev.get("ts"), "text": (ev.get("text") or "")[-text_max:]}
    row["stale"] = stale
    return row


def _etag(parts: list[str]) -> str:
    return '"' + hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32] + '"'


def _identity(opened: _Opened) -> str:
    st = opened.st
    return f"{opened.path.name}:{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


# ── DB side ─────────────────────────────────────────────────────────────────


def applies(adapter: Any) -> bool:
    return adapter.find_task_sessions is not None and adapter.tasks_in_own_session()


def _aware(dt: Any) -> datetime | None:
    if isinstance(dt, str):
        return _parse_ts(dt)
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Any) -> str | None:
    aware = _aware(dt)
    return aware.isoformat() if aware else None


def _live_state():
    return or_(Task.status.in_(LIVE_STATUSES), and_(Task.status == "inbox", Task.dispatched_at.is_not(None)))


def _live_clause(agent_id: Any):
    return and_(Task.assigned_agent_id == agent_id, _live_state())


def _not_live_clause(agent_id: Any):
    """NOT :func:`_live_clause`, NULL-safe: a task with no assignee (or
    another one) is "not live here" — a plain ``NOT`` would drop it as
    NULL."""
    return or_(Task.assigned_agent_id.is_(None), Task.assigned_agent_id != agent_id, ~_live_state())


def is_live(task: Task, agent_id: Any) -> bool:
    return task.assigned_agent_id == agent_id and (
        task.status in LIVE_STATUSES or (task.status == "inbox" and task.dispatched_at is not None)
    )


async def related_task(session: AsyncSession, agent: Agent, task_id: Any) -> Task | None:
    """The task when it is assigned to ``agent`` or was worked by it (an
    ``→ in_progress`` event with its ``agent_id``), else ``None`` (N13)."""
    task = await session.get(Task, task_id)
    if task is None:
        return None
    if task.assigned_agent_id == agent.id:
        return task
    worked = (
        await session.exec(
            select(TaskEvent.id).where(
                TaskEvent.task_id == task.id,
                TaskEvent.agent_id == agent.id,
                TaskEvent.to_status == "in_progress",
            ).limit(1)
        )
    ).first()
    return task if worked is not None else None


async def _outcomes(session: AsyncSession, tasks: list[Task], live_ids: set[Any]) -> dict[Any, str | None]:
    """One outcome line per task, from the same FIRST-choice source as the
    task detail's state card (spec A §4.1.6); fallbacks stay one tap away."""
    from app.services.messaging import open_questions, pick_open_question

    if not tasks:
        return {}
    ids = [t.id for t in tasks]
    comments = (
        await session.exec(
            select(TaskComment.task_id, TaskComment.comment_type, TaskComment.content)
            .where(TaskComment.task_id.in_(ids), TaskComment.comment_type.in_(("resolution", "blocker")))
            .order_by(TaskComment.created_at.desc())
        )
    ).all()
    newest: dict[tuple[Any, str], str] = {}
    for task_id, ctype, content in comments:
        if (content or "").strip():
            newest.setdefault((task_id, ctype), content)
    out: dict[Any, str | None] = {}
    for task in tasks:
        text: str | None = None
        if task.status in ("done", "review"):
            text = newest.get((task.id, "resolution"))
        elif task.status in ("failed", "aborted"):
            text = newest.get((task.id, "blocker"))
        elif task.status in ("waiting", "blocked", "user_test"):
            if task.id in live_ids and task.thread_id is not None:
                question = pick_open_question(await open_questions(session, thread_id=task.thread_id))
                if question is not None:
                    text = question.body
            if text is None:
                text = newest.get((task.id, "blocker"))
        out[task.id] = plain_line(text, _OUTCOME_MAX)
    return out


async def task_stubs(session: AsyncSession, agent: Agent, limit: int = 20) -> dict[str, Any]:
    """The tasks of this chat (spec A §4.1, D8/M5).

    * LIVE: assigned here AND in a live state (or delivered). Own query, no
      page limit — a live task never falls out because it is old.
    * Others: assigned here with a start, OR worked here (an ``→ in_progress``
      event with this agent's id: handed over to someone else since) — the
      newest ``limit`` by ``startKey``. A handed-over task is always a card,
      with ``assignee`` = the agent that has it now.
    ``startKey = coalesce(started_at, first → in_progress event,
    dispatched_at)`` survives park/requeue resetting the task's own times."""
    adapter = adapter_for(agent)
    if not applies(adapter):
        return dict(NOT_APPLICABLE)

    live_tasks = list(
        (
            await session.exec(
                select(Task)
                .where(_live_clause(agent.id))
                .order_by(func.coalesce(Task.started_at, Task.dispatched_at).desc())
                .limit(_LIVE_CAP)
            )
        ).all()
    )
    live_ids = {t.id for t in live_tasks}

    first_ip = (
        select(TaskEvent.task_id.label("task_id"), func.min(TaskEvent.created_at).label("first_ip"))
        .where(TaskEvent.to_status == "in_progress")
        .group_by(TaskEvent.task_id)
        .subquery()
    )
    worked_here = select(TaskEvent.task_id).where(
        TaskEvent.agent_id == agent.id, TaskEvent.to_status == "in_progress"
    )
    start_key = func.coalesce(Task.started_at, first_ip.c.first_ip, Task.dispatched_at)
    stmt = (
        select(Task, first_ip.c.first_ip)
        .outerjoin(first_ip, first_ip.c.task_id == Task.id)
        .where(
            or_(
                and_(Task.assigned_agent_id == agent.id, start_key.is_not(None)),
                Task.id.in_(worked_here),
            ),
            # Live tasks are excluded HERE, not by the capped live list: a
            # live task beyond the defensive cap must never come back as a
            # finished-looking card (review on #935).
            _not_live_clause(agent.id),
        )
        .order_by(start_key.desc())
        .limit(limit + 1)
    )
    rows = list((await session.exec(stmt)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    first_ip_of: dict[Any, Any] = {t.id: fip for t, fip in rows}
    if live_ids:
        for task_id, fip in (
            await session.exec(
                select(TaskEvent.task_id, func.min(TaskEvent.created_at))
                .where(TaskEvent.task_id.in_(live_ids), TaskEvent.to_status == "in_progress")
                .group_by(TaskEvent.task_id)
            )
        ).all():
            first_ip_of[task_id] = fip
    others = [t for t, _fip in rows]
    every = live_tasks + others
    ids = [t.id for t in every]

    left_ip: dict[Any, Any] = {}
    finished_ids = [t.id for t in others if t.completed_at is None]
    if finished_ids:
        for task_id, at in (
            await session.exec(
                select(TaskEvent.task_id, func.max(TaskEvent.created_at))
                .where(TaskEvent.task_id.in_(finished_ids), TaskEvent.from_status == "in_progress")
                .group_by(TaskEvent.task_id)
            )
        ).all():
            left_ip[task_id] = at

    # The current run (round-7 re-check t1 NICE-3): after "Request changes"
    # the end line said "took 9 min" — first start to the end of run 2, the
    # wait for the review included. The newest START of a run begins the run
    # the line names; ``startKey`` keeps the first start (it places the task
    # in the chat). A start is a step ``inbox → in_progress`` (first start,
    # rework start, pull claim, orphan recovery, phase advance) or a phase
    # rewrite (``done → in_progress``, no inbox step). NOT a start: an
    # answered question / Telegram resume (``blocked → in_progress``), a
    # reviewer taking the card, or the provisional ``review → in_progress``
    # of a drag/PATCH rejection that then goes via inbox (round-8 review
    # MUST-2: "Dauer 1 min (Lauf 2)" after 20 min of work and one question).
    run_of: dict[Any, tuple[Any, int]] = {}
    if ids:
        for task_id, newest, count in (
            await session.exec(
                select(TaskEvent.task_id, func.max(TaskEvent.created_at), func.count())
                .where(
                    TaskEvent.task_id.in_(ids),
                    TaskEvent.to_status == "in_progress",
                    or_(
                        TaskEvent.from_status == "inbox",
                        TaskEvent.reason == RUN_START_REWRITE_REASON,
                    ),
                )
                .group_by(TaskEvent.task_id)
            )
        ).all():
            run_of[task_id] = (newest, int(count or 0))

    deliverables: dict[Any, int] = {}
    if ids:
        for task_id, count in (
            await session.exec(
                select(TaskDeliverable.task_id, func.count())
                .where(TaskDeliverable.task_id.in_(ids))
                .group_by(TaskDeliverable.task_id)
            )
        ).all():
            deliverables[task_id] = int(count or 0)

    holder_ids = {t.assigned_agent_id for t in others if t.assigned_agent_id not in (None, agent.id)}
    holders: dict[Any, str] = {}
    if holder_ids:
        for aid, name in (await session.exec(select(Agent.id, Agent.name).where(Agent.id.in_(holder_ids)))).all():
            holders[aid] = name

    outcomes = await _outcomes(session, every, live_ids)

    queued = (
        await session.exec(
            select(func.count()).select_from(Task).where(
                Task.assigned_agent_id == agent.id,
                Task.status == "inbox",
                Task.dispatched_at.is_(None),
                # Same filter as the poll claim: a held / stopped card never
                # starts by itself, so it is not "waiting" (review Z4).
                Task.run_control.is_(None),
            )
        )
    ).one()

    def stub(task: Task) -> dict[str, Any]:
        live = task.id in live_ids
        start = task.started_at or first_ip_of.get(task.id) or task.dispatched_at
        ended = task.completed_at or (None if live else left_ip.get(task.id))
        assignee = None
        if not live and task.assigned_agent_id not in (None, agent.id):
            assignee = holders.get(task.assigned_agent_id)
        return {
            "taskId": str(task.id),
            "title": task.title,
            "status": task.status,
            "live": live,
            "deliveredAt": _iso(task.dispatched_at),
            "startedAt": _iso(task.started_at),
            "startKey": _iso(start),
            "runStartedAt": _iso(run_of.get(task.id, (None, 0))[0]),
            "runs": run_of.get(task.id, (None, 0))[1],
            "endedAt": _iso(ended),
            "assignee": assignee,
            "outcome": outcomes.get(task.id),
            "deliverables": deliverables.get(task.id, 0),
            # A review run of this agent works the card (round-4 review r1
            # S2): not a live section, but the chat's task turn is real —
            # the status line must not call it ready.
            "reviewRun": review_run_holds(task, agent),
            # Why the system made the card (e.g. "bench_studio challenge
            # <id>"): a bench run's card leads to its benchmark result.
            "autoReason": task.auto_reason,
        }

    return {
        "applies": True,
        "tasks": [stub(t) for t in every],
        "queued": int(queued or 0),
        "hasMore": has_more,
    }


def body_etag(body: dict[str, Any]) -> str:
    return _etag([json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)])


# ── steps + detail (files; run in a thread) ─────────────────────────────────


@dataclass
class StepsQuery:
    limit: int = 200
    after: str | None = None
    open_ids: tuple[str, ...] = ()


@dataclass
class Answer:
    """``status`` 200/304/404 + body + ETag (``None`` = none)."""

    status: int
    body: dict[str, Any] | None
    etag: str | None = None


_NO_TRANSCRIPT: dict[str, Any] = {"reason": "no_transcript"}


def _task_since(task: Task) -> float | None:
    created = _aware(getattr(task, "created_at", None))
    return (created - _SINCE_SLACK).timestamp() if created else None


def _open_attempts(agent: Any, adapter: Any, tdir: Path, task: Task) -> tuple[list[tuple[int, _Opened]], bool]:
    """(``(attempt number, opened file)`` of every attempt this agent may
    show, whether any file was found at all). The number is the attempt's
    place among ALL the task's files — a hidden attempt keeps the others'
    numbers (and row ids) stable.

    A file that does not open as the one discovery saw is skipped, and the
    task's discovery is dropped so the next poll looks again (a file
    legitimately replaced then gets its new identity pinned)."""
    files = _discover(adapter, tdir, str(task.id), _task_since(task))
    opened: list[tuple[int, _Opened]] = []
    refused = False
    for n, found in enumerate(files, start=1):
        if not adapter.transcript_allowed(agent, found.path):
            continue
        handle = open_pinned(tdir, found.path, found.identity)
        if handle is None:
            refused = True
            continue
        opened.append((n, handle))
    if refused:
        _forget(tdir, str(task.id))
    return opened, bool(files)


def _close_all(handles: list[tuple[int, _Opened]]) -> None:
    for _n, h in handles:
        h.close()


def task_steps(agent: Any, task: Task, live: bool, query: StepsQuery, if_none_match: str | None) -> Answer:
    """The steps page of ``task`` (spec A §4.2). Sync: call in a thread."""
    adapter = adapter_for(agent)
    tdir = adapter.resolve_transcript_dir(agent)
    if tdir is None:
        return Answer(404, dict(_NO_TRANSCRIPT))
    handles, found = _open_attempts(agent, adapter, tdir, task)
    if found and not handles:
        return Answer(404, dict(_NO_TRANSCRIPT))

    preview: _Opened | None = None
    try:
        if live and handles:
            preview = _open_preview(agent, adapter, tdir, handles[-1][1].path)
        parts = [f"{n}:{_identity(h)}" for n, h in handles]
        if preview is not None:
            stale = _now() - preview.st.st_mtime > _STALE_S
            parts.append(f"preview:{preview.st.st_size}:{preview.st.st_mtime_ns}:{stale}")
        parts += [str(task.status), str(getattr(task, "updated_at", "")), str(live),
                  str(query.limit), query.after or "", ",".join(sorted(query.open_ids))]
        tag = _etag(parts)
        if if_none_match == tag:
            return Answer(304, None, tag)

        base = {"applies": True, "taskId": str(task.id), "status": task.status, "live": live}
        if not handles:
            return Answer(200, {**base, "hasSession": False, "attempts": [], "rows": [], "updated": [],
                                "cursor": "v1", "hasMore": False, "reset": False, "omitted": 0,
                                "liveRow": None}, tag)

        numbers = [n for n, _h in handles]
        attempts = [_attempt_rows(h, adapter, n) for n, h in handles]
        page = page_rows([a.rows for a in attempts], [a.count for a in attempts], query.after, query.limit)
        sent = {row["id"] for row in page["rows"]}
        current = {row["id"]: row for a in attempts for _i, row in a.rows}
        updated = [current[i] for i in query.open_ids if i in current and i not in sent]
        live_row = None
        if preview is not None:
            live_row = _live_row(preview, attempts[-1].newest_ts, _LIVE_TEXT_MAX)
            preview = None  # closed by _live_row
        return Answer(200, {
            **base,
            "hasSession": True,
            "attempts": [{"n": n, "startedAt": a.started_at} for n, a in zip(numbers, attempts)],
            "rows": page["rows"],
            "updated": updated,
            "cursor": page["cursor"],
            "hasMore": page["hasMore"],
            "reset": page["reset"],
            "omitted": page["omitted"],
            "liveRow": live_row,
        }, tag)
    finally:
        _close_all(handles)
        if preview is not None:
            preview.close()


def _clamped(text: str) -> dict[str, Any]:
    capped, truncated = _cap(text, _DETAIL_TEXT_MAX)
    return {"text": capped, "truncated": truncated}


def _clamp_values(value: Any) -> Any:
    """A tool's arguments with every string clamped to
    :data:`_DETAIL_TEXT_MAX` (a write tool can carry megabytes of content).
    Shape kept; nothing else touched."""
    if isinstance(value, str):
        return value if len(value) <= _DETAIL_TEXT_MAX else value[:_DETAIL_TEXT_MAX]
    if isinstance(value, dict):
        return {k: _clamp_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clamp_values(v) for v in value]
    return value


def _detail_of(events: list[dict[str, Any]], attempt: int, index: int, piece: int | None) -> dict[str, Any] | None:
    if index >= len(events):
        return None
    ev = events[index]
    row_id = f"{attempt}:{index}" + (f".{piece}" if piece is not None else "")
    base = {"id": row_id, "ts": ev.get("ts")}
    kind = ev.get("kind")
    turn = next((j for j in range(index, -1, -1) if _starts_turn(events[j])), 0)
    thoughts = [e.get("text") or "" for e in events[turn:index]
                if e.get("kind") == "thinking" and (e.get("text") or "").strip()]
    if piece is not None:
        if kind != "message" or ev.get("role") != "assistant":
            return None
        split = split_glued(ev.get("text") or "", thoughts)
        if split is None or piece >= len(split.pieces):
            return None
        return {**base, "k": "text", **_clamped(split.pieces[piece][1])}
    if kind == "thinking":
        out = {**base, "k": "think", "text": ev.get("text") or ""}
        if isinstance(ev.get("durationMs"), int):
            out["durationMs"] = ev["durationMs"]
        return out
    if kind == "tool":
        out = {**base, "k": "tool", "name": ev.get("name"), "title": ev.get("title"), "st": _tool_state(ev),
               "detail": _clamp_values(ev.get("detail")), "result": ev.get("result")}
        for key in ("diffs", "locations", "stats"):
            if ev.get(key):
                out[key] = ev[key]
        comment = comment_of(ev)
        if comment is not None:
            out.update({"k": "comment", "type": comment["type"], "text": comment["text"]})
        return out
    if kind == "message":
        if ev.get("role") == "assistant":
            if split_glued(ev.get("text") or "", thoughts) is not None:
                return None  # the glued line itself is never a row
            return {**base, "k": "text", **_clamped(ev.get("text") or "")}
        if _is_delivery(ev):
            first = next(
                (i for i, e in enumerate(events) if e.get("kind") == "message" and _is_delivery(e)), None
            )
            if first == index:
                kind_name = "delivery" if attempt == 1 else "attempt"
                return {**base, "k": kind_name, **_clamped(ev.get("text") or "")}
        return {**base, "k": "system", **_clamped(str(ev.get("text") or ""))}
    return None


def step_detail(agent: Any, task: Task, live: bool, row_id: str, if_none_match: str | None) -> Answer:
    """One row in full (spec A §4.3). Sync: call in a thread. ``row_id`` is
    already checked against :data:`ROW_ID_RE` (or is ``"live"``)."""
    adapter = adapter_for(agent)
    tdir = adapter.resolve_transcript_dir(agent)
    if tdir is None:
        return Answer(404, dict(_NO_TRANSCRIPT))
    handles, _found = _open_attempts(agent, adapter, tdir, task)
    preview: _Opened | None = None
    try:
        if not handles:
            return Answer(404, dict(_NO_TRANSCRIPT))
        if row_id == "live":
            last = handles[-1][1]
            if live:
                preview = _open_preview(agent, adapter, tdir, last.path)
            parts = [_identity(last), "live", str(live), str(task.status)]
            if preview is not None:
                stale = _now() - preview.st.st_mtime > _STALE_S
                parts.append(f"preview:{preview.st.st_size}:{preview.st.st_mtime_ns}:{stale}")
            tag = _etag(parts)
            if if_none_match == tag:
                return Answer(304, None, tag)
            if preview is None:
                return Answer(200, {"liveRow": None}, tag)
            # The parse cache (keyed by the file's identity, size and mtime):
            # while only the preview changes this is a hit, not a parse of a
            # tens-of-MB attempt file on every 2 s poll.
            newest = _attempt_rows(last, adapter, handles[-1][0]).newest_ts
            row = _live_row(preview, newest, _DETAIL_LIVE_MAX)
            preview = None
            return Answer(200, {"liveRow": row}, tag)

        head, _, piece_s = row_id.partition(".")
        attempt_s, _, index_s = head.partition(":")
        attempt, index = int(attempt_s), int(index_s)
        piece = int(piece_s) if piece_s else None
        handle = next((h for n, h in handles if n == attempt), None)
        if handle is None:
            return Answer(404, dict(_NO_TRANSCRIPT))
        tag = _etag([_identity(handle), row_id])
        if if_none_match == tag:
            return Answer(304, None, tag)
        events, _started = _read_events(handle, adapter)
        detail = _detail_of(events, attempt, index, piece)
        if detail is None:
            return Answer(404, dict(_NO_TRANSCRIPT))
        return Answer(200, detail, tag)
    finally:
        _close_all(handles)
        if preview is not None:
            preview.close()
