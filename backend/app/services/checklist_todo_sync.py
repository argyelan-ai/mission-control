"""The model's todo list becomes the task's checklist — one list, not two.

Operator decision 12.10.2026: when an agent's chat session works on a task,
the todo snapshot it keeps (Claude Code ``TodoWrite``, an ACP ``plan``, any
harness ``chat_todos`` understands) creates and updates that task's
checklist. The chat's progress panel and the task detail then show the same
items with the same component.

Rules (all tested in tests/test_checklist_todo_sync.py):

* **Identity.** A synced row is ``source="agent_todo"`` with ``source_key`` =
  the item text, whitespace-collapsed and case-folded; a text that appears
  twice in one snapshot gets ``#2``, ``#3``. Text, not position: models
  insert steps mid-list all the time, and a position key would silently
  re-label every later row. The price: a reworded item is a new row (the
  old one is marked skipped, see below).
* **Idempotent.** Applying the same snapshot again changes nothing; the
  unique ``(task_id, source_key)`` constraint backs this up.
* **Manual rows are never touched.** Rows written by a person or an explicit
  ``mc checklist`` call (``source="manual"``) are never deleted, renamed or
  re-statused. If the snapshot names the same text as a manual row, no
  synced twin is created — the manual row already covers it.
* **Removed items are skipped, not deleted.** A synced row that left the
  snapshot and is still open becomes ``skipped``: the history stays visible
  in the task, and a skipped row does not count as open anywhere. A row
  already ``done`` stays done. If the item comes back, it is revived.
* **Who wins on conflict.** The model owns its rows, but only CHANGES of the
  snapshot are applied (``ChatTodoSync`` remembers the last applied snapshot
  per task). An agent that ticks a synced row by hand via ``mc checklist``
  keeps that state until the model writes a different list.
* **Completion guard.** ``work_context.validate_task_completion`` counts
  manual rows only. The todo list is the model's working memory, and the
  sync runs on a timer: a finish right after the last ``TodoWrite`` would
  otherwise be refused for rows the model already completed.

When does it run? ``ChatTodoSync`` (a background loop, setting
``chat_todo_sync_interval``) takes every agent whose ``current_task_id``
points at a task in ``in_progress`` assigned to it, PLUS every task that left
``in_progress`` within ``FINAL_SYNC_LOOKBACK`` (from ``task_events``, worker =
the agent of the last transition into in_progress) — so a card that finishes
between two ticks still gets its final list.

Which transcript? The TASK's, not "the active chat session" (live failure
12.10.2026: an ACP agent's chat session and its task runs are different
files; the plan sat in the task run, the sync read the chat, 0 rows). A
session file belongs to a task when the omp bridge's session header carries
``taskId`` (every attempt of a task is a new ACP session) or a user turn
carries the dispatch prompt's ``**Task ID:** <uuid>`` (Claude Code, older
bridge images). Inside one file, a todo belongs to the task named last
before it — a Claude Code session can work several tasks in a row. Only when
no file names the task does the sync fall back to the active session's
untagged list. All reads go through the chat view's adapter + privacy gate
(``transcript_adapters.adapter_for``), and only snapshots written AFTER the
task was dispatched count. While an agent works a task, its chat panel shows
that task's list too (``chat_todos`` for the history page, a live
``chat_event`` push from the loop). Heads are not synced yet (they have no
``current_task_id``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.checklist import SOURCE_AGENT_TODO, TaskChecklistItem
from app.utils import utcnow

logger = logging.getLogger("mc.checklist_todo_sync")

#: Snapshot status -> checklist status.
_STATUS = {"pending": "pending", "in_progress": "in_progress", "completed": "done"}
#: Synced rows sort after manual ones (which `mc checklist` numbers from 0).
SORT_BASE = 1000
_KEY_MAX = 300

#: Cheap pre-filter: only lines that can carry a todo list are JSON-parsed.
_TODO_LINE = re.compile(rb"(?i)todo_?write|todo-write|update_?plan|acp-plan")


def item_key(content: str) -> str:
    """Stable identity of one todo item text (see module docstring)."""
    return " ".join(content.split()).casefold()[:_KEY_MAX]


def _recount(task: Any, rows: list[TaskChecklistItem]) -> None:
    task.checklist_total = len(rows)
    task.checklist_done = sum(1 for r in rows if r.status == "done")


async def apply_todo_snapshot(
    session: AsyncSession, task: Any, items: list[dict[str, Any]], agent_id: uuid.UUID | None
) -> bool:
    """Mirror one normalised todo snapshot (``chat_todos`` items) into the
    task's checklist. Returns whether anything changed. Does not commit —
    the caller owns the transaction."""
    rows = list(
        (await session.exec(select(TaskChecklistItem).where(TaskChecklistItem.task_id == task.id))).all()
    )
    synced = {r.source_key: r for r in rows if r.source == SOURCE_AGENT_TODO and r.source_key}
    manual_keys = {item_key(r.title) for r in rows if r.source != SOURCE_AGENT_TODO}

    changed = False
    seen: set[str] = set()
    counts: dict[str, int] = {}
    now = utcnow()

    for index, item in enumerate(items):
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        base = item_key(content)
        counts[base] = counts.get(base, 0) + 1
        key = base if counts[base] == 1 else f"{base}#{counts[base]}"
        seen.add(key)
        status = _STATUS.get(item.get("status"), "pending")
        sort_order = SORT_BASE + index

        row = synced.get(key)
        if row is None:
            if counts[base] == 1 and base in manual_keys:
                continue  # a manual row already covers this step — never twin it
            row = TaskChecklistItem(
                task_id=task.id,
                agent_id=agent_id,
                title=content.strip(),
                status=status,
                sort_order=sort_order,
                source=SOURCE_AGENT_TODO,
                source_key=key,
                completed_at=now if status == "done" else None,
            )
            session.add(row)
            rows.append(row)
            synced[key] = row
            changed = True
            continue

        if row.status != status:
            row.completed_at = now if status == "done" else None
            row.status = status
            changed = True
        if row.sort_order != sort_order:
            row.sort_order = sort_order
            changed = True
        if row.title != content.strip():
            row.title = content.strip()  # same key, new spelling (case/spaces)
            changed = True
        session.add(row)

    for key, row in synced.items():
        if key not in seen and row.status not in ("done", "skipped"):
            row.status = "skipped"
            row.completed_at = None
            session.add(row)
            changed = True

    if changed:
        _recount(task, rows)
        session.add(task)
        await session.flush()
    return changed


# ── the loop ────────────────────────────────────────────────────────────────

#: Where a harness keeps its session files below the transcript root: flat
#: (Claude Code), one per-cwd folder deep (omp, Hermes), or — for the omp
#: bridge's display copy, the only file that carries a task run's
#: ``taskId`` and plan — in that folder's ``.mc/`` (``omp_chat.
#: _MC_SINK_DIRNAME``; old copies still lie beside omp's file until they are
#: moved, so both places are read). Other deeper files are subagents,
#: previews, media or blobs — never a task's own session.
_SESSION_GLOBS = ("*.jsonl", "*/*.jsonl", "*/.mc/*.jsonl")

#: Lines that can name the task a session works on: the omp bridge's session
#: header (``taskId``) or the dispatch prompt (``**Task ID:** <uuid>``).
_TASK_LINE = re.compile(rb'Task ID:\*\* |"taskId"')
_DISPATCH_MARKER = re.compile(
    r"\*\*Task ID:\*\* ([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

#: How long after a task left ``in_progress`` (review/done/blocked/...) the
#: loop still settles its checklist with the final snapshot. Several ticks
#: on purpose: a bind mount can show a file's last bytes a moment late, and
#: re-applying the same snapshot is free (digest + idempotent apply).
FINAL_SYNC_LOOKBACK = timedelta(minutes=10)
#: For the untagged fallback only: a list written this long after the task
#: left in_progress still counts (the agent's last TodoWrite and its
#: ``mc review`` can land in either order within one turn).
FINAL_SYNC_GRACE = timedelta(seconds=60)
#: A session file last written this long before the task started cannot
#: hold the task's list (mtime is only a pre-filter; content decides).
_MTIME_SLACK = timedelta(minutes=2)
_MAX_CURSORS = 256


@dataclass
class _Cursor:
    """Incremental reader of ONE session file.

    Every todo snapshot is filed under the task whose marker came last
    before it in the file (``None`` = no task named yet — a plain chat).
    One Claude Code session can work several tasks in a row; an omp bridge
    run names its task once, in the header."""

    path: Path
    offset: int = 0
    buffer: bytes = b""
    parser: Callable[..., list[dict[str, Any]]] | None = None
    segment: str | None = None
    tasks: set[str] = field(default_factory=set)
    latest: dict[str | None, dict[str, Any]] = field(default_factory=dict)


def _parse_ts(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
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


def _task_window_start(task: Any) -> datetime | None:
    """A snapshot counts for this task only if written after its latest
    start (re-dispatch included)."""
    stamps = [_aware(getattr(task, a, None)) for a in ("dispatched_at", "started_at")]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else _aware(getattr(task, "created_at", None))


def _task_marker(obj: dict[str, Any]) -> str | None:
    """The task id a transcript entry declares, or ``None``.

    Two sources, both written BEFORE the work they name:
      * the omp bridge's session header of a task run (``taskId``) — every
        attempt of a task (continue nudge, reflection retry) is a new ACP
        session without the dispatch prompt, so the header is what ties it
        to the task;
      * the dispatch prompt in a USER turn (``**Task ID:** <uuid>``,
        ``dispatch_message_builder``) — Claude Code and older bridge images.
    Tool results and assistant text never count: an agent that prints a
    dispatch (``mc task show``) does not start working that task."""
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
            match = _DISPATCH_MARKER.search(text)
            if match:
                return match.group(1).lower()
    return None


def _newer(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    if a is None:
        return b
    if b is None:
        return a
    ta, tb = _parse_ts(a.get("ts")), _parse_ts(b.get("ts"))
    if ta is None:
        return b
    if tb is None:
        return a
    return b if tb >= ta else a


def _in_window(todo: dict[str, Any] | None, start: datetime | None, until: datetime | None) -> bool:
    if todo is None:
        return False
    written = _parse_ts(todo.get("ts"))
    if written is None:
        return False
    if start is not None and written < start:
        return False
    return until is None or written <= until


@dataclass
class ChatTodoSync:
    """Background loop; also callable once (``sync_once``) for tests."""

    _cursors: dict[Path, _Cursor] = field(default_factory=dict)
    _applied: dict[str, str] = field(default_factory=dict)
    _broadcast: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _task: asyncio.Task | None = None
    _running: bool = False

    # ── reading (sync I/O — always via asyncio.to_thread) ──────────────────

    def _advance(self, path: Path, adapter: Any) -> _Cursor:
        cursor = self._cursors.get(path)
        if cursor is None:
            cursor = _Cursor(path=path, parser=adapter.new_parser(path))
            self._cursors[path] = cursor
            while len(self._cursors) > _MAX_CURSORS:
                self._cursors.pop(next(iter(self._cursors)))
        try:
            with open(path, "rb") as fh:
                size = fh.seek(0, 2)
                if size < cursor.offset:  # truncated/replaced: start over
                    cursor.offset, cursor.buffer, cursor.segment = 0, b"", None
                    cursor.tasks.clear()
                    cursor.latest.clear()
                fh.seek(cursor.offset)
                chunk = fh.read()
        except OSError:
            return cursor
        cursor.offset += len(chunk)
        lines = (cursor.buffer + chunk).split(b"\n")
        cursor.buffer = lines.pop()
        for raw in lines:
            names_task = _TASK_LINE.search(raw) is not None
            has_todo = _TODO_LINE.search(raw) is not None
            if not (names_task or has_todo):
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if names_task:
                try:
                    obj = json.loads(text)
                except ValueError:
                    obj = None
                marker = _task_marker(obj) if isinstance(obj, dict) else None
                if marker is not None:
                    cursor.segment = marker
                    cursor.tasks.add(marker)
            if has_todo:
                for ev in cursor.parser(text, None):
                    if ev.get("kind") == "todo":
                        cursor.latest[cursor.segment] = ev
        return cursor

    def _session_files(self, root: Path, since: datetime | None) -> list[Path]:
        floor = (since - _MTIME_SLACK).timestamp() if since is not None else None
        found: list[Path] = []
        for pattern in _SESSION_GLOBS:
            for path in root.glob(pattern):
                try:
                    if floor is not None and path.stat().st_mtime < floor:
                        continue
                except OSError:
                    continue
                found.append(path)
        return found

    def find_task_todo(
        self,
        agent: Any,
        adapter: Any,
        task_id: str,
        since: datetime | None,
        until: datetime | None = None,
    ) -> dict[str, Any] | None:
        """The newest todo snapshot the agent wrote WHILE working this task.

        Read from every session file that names the task (bridge header or
        dispatch prompt) — the task's own runs, not "the newest session":
        an ACP agent's chat session and its task runs are different files
        (live 12.10.2026: the plan sat in the task run, the sync read the
        chat and found nothing). Only when no file names the task does it
        fall back to the active session's untagged list (``until`` bounds
        that fallback for a task that already finished)."""
        root = adapter.resolve_transcript_dir(agent)
        if root is None:
            return None
        task_id = task_id.lower()
        with self._lock:
            best: dict[str, Any] | None = None
            tagged = False
            for path in self._session_files(root, since):
                if not adapter.transcript_allowed(agent, path):
                    continue
                cursor = self._advance(path, adapter)
                if task_id not in cursor.tasks:
                    continue
                tagged = True
                todo = cursor.latest.get(task_id)
                if _in_window(todo, since, None):
                    best = _newer(best, todo)
            if tagged:
                return best
            found = adapter.find_active_session(root)
            if not found or not adapter.transcript_allowed(agent, found[0]):
                return None
            todo = self._advance(found[0], adapter).latest.get(None)
            return todo if _in_window(todo, since, until) else None

    # ── which tasks ────────────────────────────────────────────────────────

    async def _recently_finished(
        self, session: AsyncSession, skip: set[uuid.UUID]
    ) -> list[tuple[Any, Any, datetime]]:
        """Tasks that LEFT in_progress within ``FINAL_SYNC_LOOKBACK``, with
        the agent that worked them and when they left.

        Read from the status event log (``task_events``), not from memory:
        the final pass must not depend on the loop having seen the task
        while it ran (a card shorter than one interval never was), and it
        survives a backend restart. The worker is the agent of the latest
        transition INTO in_progress — after ``review`` the assignee may
        already be the reviewer."""
        from app.models.agent import Agent
        from app.models.task import Task, TaskEvent

        cutoff = utcnow() - FINAL_SYNC_LOOKBACK
        leaves = (
            await session.exec(
                select(TaskEvent)
                .where(
                    TaskEvent.from_status == "in_progress",
                    TaskEvent.to_status != "in_progress",
                    TaskEvent.created_at >= cutoff,
                )
                .order_by(TaskEvent.created_at)
            )
        ).all()
        last_leave: dict[uuid.UUID, Any] = {}
        for ev in leaves:
            last_leave[ev.task_id] = ev

        out: list[tuple[Any, Any, datetime]] = []
        for task_id, leave in last_leave.items():
            if task_id in skip:
                continue
            task = await session.get(Task, task_id)
            if task is None or task.status == "in_progress":
                continue
            start = (
                await session.exec(
                    select(TaskEvent)
                    .where(
                        TaskEvent.task_id == task_id,
                        TaskEvent.to_status == "in_progress",
                        TaskEvent.agent_id.is_not(None),
                        TaskEvent.created_at <= leave.created_at,
                    )
                    .order_by(TaskEvent.created_at.desc())
                    .limit(1)
                )
            ).first()
            worker_id = (start.agent_id if start else None) or leave.agent_id or task.assigned_agent_id
            agent = await session.get(Agent, worker_id) if worker_id else None
            if agent is None:
                continue
            out.append((agent, task, _aware(leave.created_at)))
        return out

    async def sync_once(self, session: AsyncSession, adapter_for: Callable[[Any], Any] | None = None) -> int:
        """One pass: every task an agent is working on, plus every task that
        just left in_progress (its final list). Returns the number of tasks
        whose checklist changed."""
        from app.models.agent import Agent
        from app.models.task import Task

        if adapter_for is None:
            from app.services.transcript_adapters import adapter_for as _default

            adapter_for = _default

        working = (
            await session.exec(
                select(Agent, Task)
                .join(Task, Agent.current_task_id == Task.id)
                .where(Task.status == "in_progress", Task.assigned_agent_id == Agent.id)
            )
        ).all()
        jobs: list[tuple[Any, Any, datetime | None, bool]] = [(a, t, None, True) for a, t in working]
        finished = await self._recently_finished(session, {t.id for _, t in working})
        jobs += [(a, t, left + FINAL_SYNC_GRACE, False) for a, t, left in finished]

        changed_tasks = 0
        for agent, task, until, live in jobs:
            try:
                todo = await asyncio.to_thread(
                    self.find_task_todo, agent, adapter_for(agent), str(task.id), _task_window_start(task), until
                )
            except Exception:  # noqa: BLE001 — one agent must not stop the pass
                logger.debug("todo sync: transcript read failed for agent %s", agent.id, exc_info=True)
                continue
            if todo is None:
                continue
            digest = json.dumps(todo.get("items") or [], sort_keys=True)
            task_key = str(task.id)
            if live and self._broadcast.get(str(agent.id)) != digest:
                await self._push_to_chat(agent.id, task.id, todo)
                self._broadcast[str(agent.id)] = digest
            if self._applied.get(task_key) == digest:
                continue
            if await apply_todo_snapshot(session, task, todo.get("items") or [], agent.id):
                await session.commit()
                changed_tasks += 1
            self._applied[task_key] = digest
        return changed_tasks

    # ── the chat panel ─────────────────────────────────────────────────────

    async def chat_todos(
        self, session: AsyncSession, agent: Any, adapter: Any, own: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The list the agent's chat panel shows: while the agent works a
        task, that task's snapshot (from the task's own session — the
        operator watches the AGENT's chat, not the task run); otherwise, or
        when the task has no list yet, the chat session's own (``own``)."""
        from app.models.task import Task

        task_id = getattr(agent, "current_task_id", None)
        if task_id is None:
            return own
        task = await session.get(Task, task_id)
        if task is None or task.status != "in_progress" or task.assigned_agent_id != agent.id:
            return own
        try:
            todo = await asyncio.to_thread(
                self.find_task_todo, agent, adapter, str(task.id), _task_window_start(task)
            )
        except Exception:  # noqa: BLE001 — the panel is a nicety, history must load
            logger.debug("todo panel: task snapshot read failed for agent %s", agent.id, exc_info=True)
            return own
        return {**todo, "taskId": str(task.id)} if todo else own

    async def _push_to_chat(self, agent_id: Any, task_id: Any, todo: dict[str, Any]) -> None:
        """Live update for an open chat: the tailer only follows the chat
        session, so a task run's new list reaches the panel through here."""
        try:
            from app.redis_client import RedisKeys
            from app.services import sse

            await sse.broadcast(
                RedisKeys.agent_chat_channel(str(agent_id)), "chat_event", {**todo, "taskId": str(task_id)}
            )
        except Exception:  # noqa: BLE001 — a missed live frame heals on reload
            logger.debug("todo sync: chat broadcast failed for agent %s", agent_id, exc_info=True)

    async def start(self) -> None:
        interval = settings.chat_todo_sync_interval
        if self._running or not interval or interval <= 0 or interval >= 99999:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(interval), name="chat_todo_sync")
        logger.info("chat todo sync started (interval=%ss)", interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self, interval: int) -> None:
        from app.database import engine

        while self._running:
            await asyncio.sleep(interval)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    await self.sync_once(session)
            except Exception:  # noqa: BLE001 — never kill the loop
                logger.exception("chat todo sync pass failed")


chat_todo_sync = ChatTodoSync()
