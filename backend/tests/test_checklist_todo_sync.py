"""The model's todo snapshot becomes/updates the task's checklist (one list).

Rules under test (operator decision 12.10.2026):
  * synced rows carry ``source="agent_todo"`` + a stable ``source_key``;
  * manual rows (``mc checklist``, UI) are never deleted or overwritten;
  * the same snapshot twice changes nothing (no duplicates);
  * status changes flow through; a synced row that left the snapshot is
    marked ``skipped`` (kept for history), never deleted;
  * ``mc finish``'s completion guard ignores synced rows (the model's own
    working list must not block a finish — and the sync lags by a tick).
"""
from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import checklist_todo_sync as sync
from app.services.transcript_adapters import adapter_for
from tests.conftest import test_engine


def _todos(*pairs):
    return [{"content": c, "status": s} for c, s in pairs]


async def _scenario(status: str = "in_progress", dispatched_at: datetime | None = None):
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:8]}"))
        s.add(Agent(id=agent_id, name="Cody", role="developer", board_id=board_id, slug="cody"))
        s.add(
            Task(
                id=task_id,
                board_id=board_id,
                title="T",
                status=status,
                assigned_agent_id=agent_id,
                dispatched_at=dispatched_at,
            )
        )
        await s.commit()
        agent = await s.get(Agent, agent_id)
        agent.current_task_id = task_id
        s.add(agent)
        await s.commit()
    return board_id, agent_id, task_id


async def _rows(task_id):
    from app.models.checklist import TaskChecklistItem

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        res = await s.exec(
            select(TaskChecklistItem)
            .where(TaskChecklistItem.task_id == task_id)
            .order_by(TaskChecklistItem.sort_order)
        )
        return list(res.all())


async def _apply(task_id, agent_id, items) -> bool:
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        changed = await sync.apply_todo_snapshot(s, task, items, agent_id)
        await s.commit()
        return changed


async def _add_manual(task_id, title, status="pending", sort_order=0):
    from app.models.checklist import TaskChecklistItem

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskChecklistItem(task_id=task_id, title=title, status=status, sort_order=sort_order))
        await s.commit()


# ── apply_todo_snapshot ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_creates_synced_rows_and_counters():
    from app.models.task import Task

    _, agent_id, task_id = await _scenario()
    changed = await _apply(task_id, agent_id, _todos(("Read code", "completed"), ("Write test", "in_progress"), ("Ship", "pending")))
    assert changed is True
    rows = await _rows(task_id)
    assert [(r.title, r.status, r.source) for r in rows] == [
        ("Read code", "done", "agent_todo"),
        ("Write test", "in_progress", "agent_todo"),
        ("Ship", "pending", "agent_todo"),
    ]
    assert rows[0].completed_at is not None
    assert all(r.agent_id == agent_id and r.source_key for r in rows)
    async with AsyncSession(test_engine) as s:
        task = await s.get(Task, task_id)
        assert (task.checklist_total, task.checklist_done) == (3, 1)


@pytest.mark.asyncio
async def test_same_snapshot_twice_creates_no_duplicates():
    _, agent_id, task_id = await _scenario()
    items = _todos(("Read code", "pending"), ("Ship", "pending"))
    assert await _apply(task_id, agent_id, items) is True
    assert await _apply(task_id, agent_id, items) is False
    # whitespace/case noise is the same item, not a new one
    assert await _apply(task_id, agent_id, _todos(("  read   CODE ", "pending"), ("Ship", "pending"))) is True
    rows = await _rows(task_id)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_status_updates_flow_through():
    _, agent_id, task_id = await _scenario()
    await _apply(task_id, agent_id, _todos(("A", "pending"), ("B", "pending")))
    await _apply(task_id, agent_id, _todos(("A", "in_progress"), ("B", "pending")))
    assert [r.status for r in await _rows(task_id)] == ["in_progress", "pending"]
    await _apply(task_id, agent_id, _todos(("A", "completed"), ("B", "in_progress")))
    rows = await _rows(task_id)
    assert [r.status for r in rows] == ["done", "in_progress"]
    assert rows[0].completed_at is not None
    # reopened -> completed_at cleared
    await _apply(task_id, agent_id, _todos(("A", "pending"), ("B", "in_progress")))
    assert (await _rows(task_id))[0].completed_at is None


@pytest.mark.asyncio
async def test_manual_items_survive_and_are_never_overwritten():
    _, agent_id, task_id = await _scenario()
    await _add_manual(task_id, "Run tests", status="pending", sort_order=0)
    await _add_manual(task_id, "Operator check", status="blocked", sort_order=1)

    # The snapshot names a manual item (same text) — no duplicate row, and
    # the manual row keeps its own status.
    await _apply(task_id, agent_id, _todos(("Run tests", "completed"), ("New step", "in_progress")))
    rows = await _rows(task_id)
    manual = {r.title: r for r in rows if r.source == "manual"}
    assert manual["Run tests"].status == "pending"
    assert manual["Operator check"].status == "blocked"
    assert [r.title for r in rows if r.source == "agent_todo"] == ["New step"]

    # An empty snapshot (the model cleared its list) leaves manual rows alone.
    await _apply(task_id, agent_id, [])
    rows = await _rows(task_id)
    assert {r.title: r.status for r in rows if r.source == "manual"} == {
        "Run tests": "pending",
        "Operator check": "blocked",
    }


@pytest.mark.asyncio
async def test_synced_item_leaving_the_snapshot_is_skipped_not_deleted():
    _, agent_id, task_id = await _scenario()
    await _apply(task_id, agent_id, _todos(("Done one", "completed"), ("Dropped", "pending"), ("Stays", "pending")))
    await _apply(task_id, agent_id, _todos(("Stays", "in_progress")))
    rows = {r.title: r.status for r in await _rows(task_id)}
    assert rows == {"Done one": "done", "Dropped": "skipped", "Stays": "in_progress"}
    # coming back revives it
    await _apply(task_id, agent_id, _todos(("Stays", "in_progress"), ("Dropped", "pending")))
    assert {r.title: r.status for r in await _rows(task_id)}["Dropped"] == "pending"


@pytest.mark.asyncio
async def test_repeated_text_in_one_snapshot_is_two_items():
    _, agent_id, task_id = await _scenario()
    await _apply(task_id, agent_id, _todos(("Run tests", "completed"), ("Fix", "completed"), ("Run tests", "pending")))
    rows = await _rows(task_id)
    assert [(r.title, r.status) for r in rows] == [("Run tests", "done"), ("Fix", "done"), ("Run tests", "pending")]
    assert len({r.source_key for r in rows}) == 3


@pytest.mark.asyncio
async def test_completion_guard_ignores_synced_rows_but_not_manual_ones():
    from app.models.task import Task
    from app.services.work_context import validate_task_completion

    _, agent_id, task_id = await _scenario()
    await _apply(task_id, agent_id, _todos(("Model step", "in_progress")))
    async with AsyncSession(test_engine) as s:
        task = await s.get(Task, task_id)
        _ok, errors = await validate_task_completion(s, task)
        assert not any("Checklist" in e for e in errors)
    await _add_manual(task_id, "Manual must-do")
    async with AsyncSession(test_engine) as s:
        task = await s.get(Task, task_id)
        _ok, errors = await validate_task_completion(s, task)
        assert any("1 Checklist-Item(s) noch offen" in e for e in errors)


@pytest.mark.asyncio
async def test_agent_bulk_create_still_dedups_against_synced_rows(client):
    """`mc checklist add` keeps working and does not duplicate a step the
    model's todo already created."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    board_id, agent_id, task_id = await _scenario()
    raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        agent.agent_token_hash = token_hash
        agent.scopes = ["tasks:read", "tasks:write"]
        s.add(agent)
        await s.commit()
    await _apply(task_id, agent_id, _todos(("Write test", "pending")))
    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist",
        json={"items": [{"title": "Write test"}, {"title": "Manual extra"}]},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 201, resp.text
    rows = await _rows(task_id)
    assert sorted((r.title, r.source) for r in rows) == [("Manual extra", "manual"), ("Write test", "agent_todo")]


# ── the loop: transcript -> checklist ───────────────────────────────────────


def _claude_todo_line(uid: str, ts: str, todos) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uid,
            "timestamp": ts,
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "tool_use", "id": f"toolu_{uid}", "name": "TodoWrite", "input": {"todos": todos}}],
            },
        }
    )


def _fake_adapter(session_file):
    base = adapter_for(None)
    return dataclasses.replace(
        base,
        resolve_transcript_dir=lambda agent: session_file.parent,
        find_active_session=lambda d: (session_file, {}),
        transcript_allowed=lambda agent, path: path == session_file,
    )


@pytest.mark.asyncio
async def test_sync_once_reads_the_transcript_and_updates_the_checklist(tmp_path):
    started = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    _, _agent_id, task_id = await _scenario(dispatched_at=started)
    f = tmp_path / "s.jsonl"
    old = (started - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    new = (started + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    # A list from BEFORE this task started (previous task, same session)
    # must never land on this task.
    f.write_text(_claude_todo_line("u-old", old, _todos(("Old task step", "pending"))) + "\n")

    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _fake_adapter(f)) == 0
    assert await _rows(task_id) == []

    with f.open("a") as fh:
        fh.write(_claude_todo_line("u-new", new, _todos(("New step", "in_progress"))) + "\n")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _fake_adapter(f)) == 1
    assert [(r.title, r.status) for r in await _rows(task_id)] == [("New step", "in_progress")]

    # nothing new in the file -> nothing written
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _fake_adapter(f)) == 0


@pytest.mark.asyncio
async def test_sync_once_skips_tasks_not_in_progress_and_forbidden_transcripts(tmp_path):
    _, _agent_id, task_id = await _scenario(status="review")
    f = tmp_path / "s.jsonl"
    f.write_text(_claude_todo_line("u1", "2026-10-12T10:00:00.000Z", _todos(("X", "pending"))) + "\n")
    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _fake_adapter(f)) == 0
    assert await _rows(task_id) == []

    _, _agent2, task2 = await _scenario()
    denied = dataclasses.replace(_fake_adapter(f), transcript_allowed=lambda agent, path: False)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: denied) == 0
    assert await _rows(task2) == []
