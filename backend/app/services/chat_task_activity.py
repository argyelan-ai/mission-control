"""What the running task does — for the agent chat's task block.

Operator finding 20.10.2026: since #898 the agent chat follows the chat
daemon's session only (right: it stopped the flip between two sessions). An
ACP agent runs a task in a session OF ITS OWN, so the delivery, the tools and
the thinking of the running task left the chat entirely — "as if nothing
happens". This module reads that task session and hands the chat ONE
separate block: never mixed into the chat timeline, no session switch.

Harness-neutral: the adapter says whether tasks run in sessions of their own
(``tasks_in_own_session``) and how to find one (``find_task_session``). The
events come from the adapter's own parser via ``read_history`` — the same
normalized events the chat renders — and the live preview from the adapter's
preview channel (a running thought is a ``preview`` with ``block:
"thinking"``). A harness whose task runs INSIDE the chat (Claude Code, omp
under the native driver, Hermes — over ACP its bridge hands the task to the
chat daemon) answers ``applies: false``: its chat shows the task.

Whether the model answers at all is not this block's business — the chat's
status line says that (#897, "Wartet auf das Modell").
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task
from app.services.transcript_adapters import adapter_for
from app.services.transcript_chat import preview_event, read_history

#: How many of the task's newest timeline events the block shows.
EVENT_LIMIT = 4
#: Timeline kinds the block shows (no user lines: the delivery has its own row).
_SHOWN_KINDS = ("tool", "thinking", "message")
#: A task session written this long before the dispatch cannot be the task's.
_SINCE_SLACK = timedelta(minutes=2)
#: A preview snapshot older than this is a dead run, not a live thought.
_PREVIEW_MAX_AGE = timedelta(minutes=10)
#: Bytes read from the END of a preview file (it can be tens of MB).
_PREVIEW_TAIL_BYTES = 512 * 1024

NOT_APPLICABLE: dict[str, Any] = {"applies": False}

#: ``find_task_session`` scans the agent's whole sessions tree (hundreds of
#: files). The answer changes only when a new attempt starts, so it is kept
#: for a few seconds per (tree, task) instead of being rescanned every poll
#: of every open chat.
_SESSION_CACHE_S = 10.0
_session_cache: dict[tuple[str, str], tuple[float, Path | None]] = {}


def _cached_task_session(adapter: Any, tdir: Path, task_id: str, since: float | None) -> Path | None:
    key = (str(tdir), task_id)
    hit = _session_cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _SESSION_CACHE_S and (hit[1] is None or hit[1].exists()):
        return hit[1]
    path = adapter.find_task_session(tdir, task_id, since)
    if len(_session_cache) > 256:
        _session_cache.clear()
    _session_cache[key] = (now, path)
    return path


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_preview(preview_file: Path) -> dict[str, Any] | None:
    """The newest ``acp-preview`` snapshot of a preview file as a ``preview``
    event, read from the file's END only. Live tool output lines are skipped
    (the running tool's card already says it runs). Never raises."""
    try:
        with open(preview_file, "rb") as fh:
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
            continue  # the cut first line, or a line being written
        if not isinstance(d, dict) or d.get("customType") != "acp-preview":
            continue
        content, ts = d.get("content"), d.get("timestamp")
        if not isinstance(content, str) or not content or not isinstance(ts, str):
            return None
        return preview_event(content, ts, d.get("data"))
    return None


def _read_task_session(adapter: Any, path: Path) -> tuple[str | None, list[dict[str, Any]], dict[str, Any] | None]:
    """(delivered-at, newest shown events, live preview) of one task session."""
    history = read_history(path, adapter, limit=1000)
    events = history.get("events") or []
    delivered = next(
        (
            e.get("ts") for e in events
            if e.get("kind") == "message" and (e.get("source") or {}).get("kind") == "task"
        ),
        None,
    )
    shown = [
        e for e in events
        if e.get("kind") in _SHOWN_KINDS
        and (e.get("kind") != "message" or e.get("role") == "assistant")
    ]
    tail = shown[-EVENT_LIMIT:]

    preview = None
    if adapter.preview_channel is not None:
        pfile = adapter.preview_channel(path)
        if pfile is not None:
            preview = last_preview(pfile)
    if preview is not None:
        # A snapshot the transcript already overtook (the thought ended in a
        # tool call, the answer landed) is not live any more — same rule as
        # the chat's own seed (useChatStream.seedSequence).
        p_ts = _parse_ts(preview.get("ts"))
        newest = max((t for t in (_parse_ts(e.get("ts")) for e in shown) if t), default=None)
        now = datetime.now(timezone.utc)
        if p_ts is None or (newest is not None and p_ts <= newest) or now - p_ts > _PREVIEW_MAX_AGE:
            preview = None
    return delivered, tail, preview


async def task_activity(session: AsyncSession, agent: Any) -> dict[str, Any]:
    """The task block's payload for ``agent`` — see the module docstring."""
    task_id = getattr(agent, "current_task_id", None)
    if task_id is None:
        return dict(NOT_APPLICABLE)
    adapter = adapter_for(agent)
    if adapter.find_task_session is None or not adapter.tasks_in_own_session():
        return dict(NOT_APPLICABLE)
    task = await session.get(Task, task_id)
    if task is None:
        return dict(NOT_APPLICABLE)

    queued = (
        await session.exec(
            select(func.count()).select_from(Task).where(
                Task.assigned_agent_id == agent.id,
                Task.status == "inbox",
                Task.id != task.id,
                # Same filter as the poll claim: a held / stopped card never
                # starts by itself, so it is not "waiting" (review Z4).
                Task.run_control.is_(None),
            )
        )
    ).one()

    started = max(
        (s for s in (_aware(task.dispatched_at), _aware(getattr(task, "started_at", None))) if s),
        default=None,
    )
    since = (started - _SINCE_SLACK).timestamp() if started else None
    delivered: str | None = started.isoformat() if started else None
    events: list[dict[str, Any]] = []
    preview: dict[str, Any] | None = None
    has_session = False

    tdir = adapter.resolve_transcript_dir(agent)
    if tdir is not None:
        path = await asyncio.to_thread(_cached_task_session, adapter, tdir, str(task.id), since)
        if path is not None and adapter.transcript_allowed(agent, path):
            has_session = True
            seen, events, preview = await asyncio.to_thread(_read_task_session, adapter, path)
            delivered = seen or delivered

    return {
        "applies": True,
        "taskId": str(task.id),
        "hasSession": has_session,
        "deliveredAt": delivered,
        "events": events,
        "preview": preview,
        "queued": int(queued or 0),
    }
