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
``chat_todo_sync_interval``) looks at every agent whose ``current_task_id``
points at a task in ``in_progress`` assigned to it, reads its active chat
transcript through the same adapter + privacy gate the chat view uses
(``transcript_adapters.adapter_for``), and applies the newest todo snapshot
written AFTER the task was dispatched — a list left over from the previous
task in the same session never lands on the new one. Heads are not synced
yet (they have no ``current_task_id``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


@dataclass
class _Cursor:
    path: Path
    offset: int = 0
    buffer: bytes = b""
    parser: Callable[..., list[dict[str, Any]]] | None = None
    latest: dict[str, Any] | None = None


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


@dataclass
class ChatTodoSync:
    """Background loop; also callable once (``sync_once``) for tests."""

    _cursors: dict[str, _Cursor] = field(default_factory=dict)
    _applied: dict[str, str] = field(default_factory=dict)
    _task: asyncio.Task | None = None
    _running: bool = False

    def _latest_todo(self, agent: Any, adapter: Any) -> dict[str, Any] | None:
        """Newest todo snapshot of the agent's ACTIVE session (sync I/O —
        call via ``asyncio.to_thread``). Reads only new bytes per tick."""
        tdir = adapter.resolve_transcript_dir(agent)
        if tdir is None:
            return None
        found = adapter.find_active_session(tdir)
        if not found:
            return None
        path = found[0]
        if not adapter.transcript_allowed(agent, path):
            return None
        agent_key = str(agent.id)
        cursor = self._cursors.get(agent_key)
        if cursor is None or cursor.path != path:
            cursor = _Cursor(path=path, parser=adapter.new_parser())
            self._cursors[agent_key] = cursor
        try:
            with open(path, "rb") as fh:
                size = fh.seek(0, 2)
                if size < cursor.offset:  # truncated/replaced: start over
                    cursor.offset, cursor.buffer, cursor.latest = 0, b"", None
                fh.seek(cursor.offset)
                chunk = fh.read()
        except OSError:
            return cursor.latest
        cursor.offset += len(chunk)
        lines = (cursor.buffer + chunk).split(b"\n")
        cursor.buffer = lines.pop()
        for raw in lines:
            if not _TODO_LINE.search(raw):
                continue
            for ev in cursor.parser(raw.decode("utf-8", errors="replace").strip(), None):
                if ev.get("kind") == "todo":
                    cursor.latest = ev
        return cursor.latest

    async def sync_once(self, session: AsyncSession, adapter_for: Callable[[Any], Any] | None = None) -> int:
        """One pass over all working agents. Returns the number of tasks
        whose checklist changed."""
        from app.models.agent import Agent
        from app.models.task import Task

        if adapter_for is None:
            from app.services.transcript_adapters import adapter_for as _default

            adapter_for = _default

        pairs = (
            await session.exec(
                select(Agent, Task)
                .join(Task, Agent.current_task_id == Task.id)
                .where(Task.status == "in_progress", Task.assigned_agent_id == Agent.id)
            )
        ).all()
        live_agents = {str(a.id) for a, _ in pairs}
        for stale in set(self._cursors) - live_agents:
            self._cursors.pop(stale, None)

        changed_tasks = 0
        for agent, task in pairs:
            try:
                todo = await asyncio.to_thread(self._latest_todo, agent, adapter_for(agent))
            except Exception:  # noqa: BLE001 — one agent must not stop the pass
                logger.debug("todo sync: transcript read failed for agent %s", agent.id, exc_info=True)
                continue
            if todo is None:
                continue
            written = _parse_ts(todo.get("ts"))
            start = _task_window_start(task)
            if written is None or (start is not None and written < start):
                continue
            digest = json.dumps(todo.get("items") or [], sort_keys=True)
            task_key = str(task.id)
            if self._applied.get(task_key) == digest:
                continue
            if await apply_todo_snapshot(session, task, todo.get("items") or [], agent.id):
                await session.commit()
                changed_tasks += 1
            self._applied[task_key] = digest
        return changed_tasks

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
