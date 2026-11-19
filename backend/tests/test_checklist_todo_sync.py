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


# ── live case 12.10.2026: the plan lives in the TASK session, not the chat ──
#
# An omp agent under ACP has (at least) two transcript files in the same
# folder: the chat daemon's session (what the chat view shows — the newest
# entry) and one bridge session per task run (dispatch prompt + every
# ``acp-plan`` line). The first sync read only the "active" chat session and
# found nothing. Shapes below are copied from the live files (redacted).


def _t0() -> datetime:
    """Dispatch time a few minutes ago: the sync pre-filters session files
    by mtime, and the fixtures are written NOW."""
    return (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _omp_header(sid: str, ts: datetime, task_id: str | None = None) -> str:
    header = {"type": "session", "version": 3, "id": sid, "timestamp": _iso(ts), "cwd": "", "bridge": "acp"}
    if task_id is not None:
        header["taskId"] = task_id
    return json.dumps(header)


def _omp_user(uid: str, ts: datetime, text: str) -> str:
    return json.dumps(
        {
            "type": "message",
            "id": uid,
            "parentId": None,
            "timestamp": _iso(ts),
            "message": {"role": "user", "content": [{"type": "text", "text": text}], "attribution": "user"},
        }
    )


def _omp_assistant(uid: str, ts: datetime, text: str) -> str:
    return json.dumps(
        {
            "type": "message",
            "id": uid,
            "parentId": None,
            "timestamp": _iso(ts),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def _acp_plan(uid: str, ts: datetime, *pairs) -> str:
    entries = [{"content": c, "status": s, "priority": "medium"} for c, s in pairs]
    return json.dumps(
        {"type": "custom", "customType": "acp-plan", "data": {"entries": entries}, "id": uid, "parentId": None, "timestamp": _iso(ts)}
    )


def _dispatch_text(task_id) -> str:
    # The head of dispatch_message_builder's prompt, as it landed live.
    return (
        "# Operating Card — Agent\n\n---\n\n# New Task: [LIVE-TEST] Todo panel\n"
        f"**Priority:** medium\n**Task ID:** {task_id}\n**Board ID:** {uuid.uuid4()}\n"
    )


def _omp_tree_adapter(root):
    from types import SimpleNamespace

    base = adapter_for(SimpleNamespace(harness="omp"))
    return dataclasses.replace(
        base,
        resolve_transcript_dir=lambda agent: root,
        transcript_allowed=lambda agent, path: root in path.parents,
    )


def _write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines))


def _live_case(tmp_path, task_id, t0, *, tag_header=False):
    """The 12.10. layout: chat session newest, plan only in the task run."""
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    chat = ws / "2026-10-12T13-54-15_chat-sess.jsonl"
    task_run = ws / "2026-10-12T13-56-11_task-sess.jsonl"
    _write(
        task_run,
        _omp_header("task-sess", t0 + timedelta(seconds=11), str(task_id) if tag_header else None),
        _omp_user("acp00001", t0 + timedelta(seconds=11), _dispatch_text(task_id)),
        _acp_plan("acp00042", t0 + timedelta(seconds=26), ("Schritt 1 lesen", "in_progress"), ("Schritt 2 rechnen", "pending")),
        _acp_plan("acp000c5", t0 + timedelta(seconds=54), ("Schritt 1 lesen", "completed"), ("Schritt 2 rechnen", "in_progress")),
    )
    # The operator's chat session: talked about the task, no plan — and its
    # last entry is the NEWEST, so it is the "active" session.
    _write(
        chat,
        _omp_header("chat-sess", t0 - timedelta(minutes=2)),
        _omp_user("acp65e444a", t0 - timedelta(minutes=1), f"Wie weit ist Task {task_id}?"),
        _omp_assistant("acp65e444b", t0 + timedelta(minutes=3), "Laeuft."),
    )
    return root, chat, task_run


@pytest.mark.asyncio
async def test_sync_reads_the_task_session_not_the_active_chat_session(tmp_path):
    t0 = _t0()
    _, _agent_id, task_id = await _scenario(dispatched_at=t0)
    root, chat, _task_run = _live_case(tmp_path, task_id, t0)
    adapter = _omp_tree_adapter(root)
    # precondition: the chat session really is the active one
    assert adapter.find_active_session(root)[0] == chat

    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: adapter) == 1
    assert [(r.title, r.status) for r in await _rows(task_id)] == [
        ("Schritt 1 lesen", "done"),
        ("Schritt 2 rechnen", "in_progress"),
    ]


@pytest.mark.asyncio
async def test_follow_up_run_tagged_by_the_bridge_header_wins(tmp_path):
    """Every attempt (continue nudge, reflection retry) is a NEW ACP session
    without the dispatch prompt; the bridge stamps the task id into its
    header so the newest plan of ANY run of this task counts."""
    t0 = _t0()
    _, _agent_id, task_id = await _scenario(dispatched_at=t0)
    root, _chat, _task_run = _live_case(tmp_path, task_id, t0)
    _write(
        root / "--workspace--" / "2026-10-12T13-58-25_retry-sess.jsonl",
        _omp_header("retry-sess", t0 + timedelta(minutes=2, seconds=25), str(task_id)),
        _omp_user("acp00001", t0 + timedelta(minutes=2, seconds=25), "Pruefe das Ergebnis und schliesse sauber ab."),
        _acp_plan("acp00048", t0 + timedelta(minutes=2, seconds=40), ("Schritt 1 lesen", "completed"), ("Schritt 2 rechnen", "completed")),
    )
    # an UNRELATED run (another task) written later must not win
    _write(
        root / "--workspace--" / "2026-10-12T13-59-00_other-sess.jsonl",
        _omp_header("other-sess", t0 + timedelta(minutes=3), str(uuid.uuid4())),
        _acp_plan("acp00099", t0 + timedelta(minutes=3, seconds=5), ("Fremder Schritt", "pending")),
    )
    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _omp_tree_adapter(root)) == 1
    assert [(r.title, r.status) for r in await _rows(task_id)] == [
        ("Schritt 1 lesen", "done"),
        ("Schritt 2 rechnen", "done"),
    ]


async def _leave_in_progress(task_id, agent_id, to_status: str, at: datetime | None = None):
    """What the agent's `mc review` does to the rows the sync looks at."""
    from app.models.agent import Agent
    from app.models.task import Task, TaskEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(task_id=task_id, from_status="inbox", to_status="in_progress",
                        changed_by="agent", agent_id=agent_id, reason="pull_dispatch",
                        created_at=(at or datetime.now(timezone.utc)) - timedelta(minutes=3)))
        s.add(TaskEvent(task_id=task_id, from_status="in_progress", to_status=to_status,
                        changed_by="agent", agent_id=agent_id, created_at=at or datetime.now(timezone.utc)))
        task = await s.get(Task, task_id)
        task.status = to_status
        s.add(task)
        agent = await s.get(Agent, agent_id)
        agent.current_task_id = None
        s.add(agent)
        await s.commit()


@pytest.mark.asyncio
async def test_task_finished_between_two_ticks_still_gets_its_final_snapshot(tmp_path):
    """Live 12.10.: the card ran ~3 min; a loop limited to `in_progress`
    misses the last plan (or the whole task when it is shorter than one
    interval). The pass also settles tasks that just LEFT in_progress."""
    t0 = _t0()
    _, agent_id, task_id = await _scenario(dispatched_at=t0)
    root, _chat, _task_run = _live_case(tmp_path, task_id, t0, tag_header=True)
    # the worker never ticked while the task was in progress
    await _leave_in_progress(task_id, agent_id, "review")

    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _omp_tree_adapter(root)) == 1
    assert [(r.title, r.status) for r in await _rows(task_id)] == [
        ("Schritt 1 lesen", "done"),
        ("Schritt 2 rechnen", "in_progress"),
    ]
    # settled once — the next pass changes nothing
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _omp_tree_adapter(root)) == 0


@pytest.mark.asyncio
async def test_task_that_left_in_progress_long_ago_is_not_touched(tmp_path):
    t0 = _t0()
    _, agent_id, task_id = await _scenario(dispatched_at=t0)
    root, _chat, _task_run = _live_case(tmp_path, task_id, t0, tag_header=True)
    await _leave_in_progress(task_id, agent_id, "done", at=datetime.now(timezone.utc) - timedelta(hours=2))
    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _omp_tree_adapter(root)) == 0
    assert await _rows(task_id) == []


@pytest.mark.asyncio
async def test_claude_session_attributes_todos_to_the_task_dispatched_before_them(tmp_path):
    """Claude Code agents work tasks inside the chat session itself; one file
    can carry several tasks. A TodoWrite belongs to the dispatch above it."""
    t0 = _t0()
    _, _agent_id, task_id = await _scenario(dispatched_at=t0)
    other = uuid.uuid4()

    def _claude_user(uid, ts, text):
        return json.dumps({"type": "user", "uuid": uid, "timestamp": _iso(ts),
                           "message": {"role": "user", "content": text}})

    f = tmp_path / "proj" / "sess.jsonl"
    _write(
        f,
        _claude_user("d1", t0, _dispatch_text(task_id)),
        _claude_todo_line("t1", _iso(t0 + timedelta(minutes=1)), _todos(("Mine", "in_progress"))),
        # a later dispatch of ANOTHER task in the same session
        _claude_user("d2", t0 + timedelta(minutes=2), _dispatch_text(other)),
        _claude_todo_line("t2", _iso(t0 + timedelta(minutes=3)), _todos(("Not mine", "pending"))),
    )
    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _fake_adapter(f)) == 1
    assert [(r.title, r.status) for r in await _rows(task_id)] == [("Mine", "in_progress")]


@pytest.mark.asyncio
async def test_chat_panel_shows_the_task_snapshot_while_the_agent_works_it(tmp_path):
    """The operator watches the AGENT's chat on the phone: while it works a
    task, the panel carries that task's list (from the task session), else
    the chat session's own."""
    from app.models.agent import Agent

    t0 = _t0()
    _, agent_id, task_id = await _scenario(dispatched_at=t0)
    root, _chat, _task_run = _live_case(tmp_path, task_id, t0)
    adapter = _omp_tree_adapter(root)
    own = {"kind": "todo", "uuid": "c1", "ts": "2026-10-12T13:00:00Z", "source": "acp_plan",
           "items": [{"content": "Chat item", "status": "pending"}]}

    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        todo = await worker.chat_todos(s, agent, adapter, own)
    assert todo is not None and todo["taskId"] == str(task_id)
    assert [i["content"] for i in todo["items"]] == ["Schritt 1 lesen", "Schritt 2 rechnen"]

    await _leave_in_progress(task_id, agent_id, "done")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = await s.get(Agent, agent_id)
        assert await worker.chat_todos(s, agent, adapter, own) == own


# ── MC's display copies in ``<folder>/.mc/`` (chat reliability S1, R1) ─────
#
# A later step moves the omp bridge's display copies one level down, into
# ``<cwd folder>/.mc/`` (omp 18.10.1 took them for its own sessions). The
# task's plan and its ``taskId`` live ONLY in that copy, so the sync must
# read it there — and, during the move, in both places at once.


@pytest.mark.asyncio
async def test_task_plan_in_a_copy_under_mc_is_found(tmp_path):
    t0 = _t0()
    _, _agent_id, task_id = await _scenario(dispatched_at=t0)
    root = tmp_path / "omp-sessions"
    ws = root / "--workspace--"
    _write(
        ws / "2026-10-12T13-54-15_chat-sess.jsonl",
        _omp_header("chat-sess", t0 - timedelta(minutes=2)),
        _omp_assistant("acp65e444b", t0 + timedelta(minutes=3), "Laeuft."),
    )
    _write(
        ws / ".mc" / "2026-10-12T13-56-11_task-sess.jsonl",
        _omp_header("task-sess", t0 + timedelta(seconds=11), str(task_id)),
        _omp_user("acp00001", t0 + timedelta(seconds=11), "Arbeite die Aufgabe ab."),
        _acp_plan("acp00042", t0 + timedelta(seconds=26), ("Schritt 1 lesen", "completed"), ("Schritt 2 rechnen", "in_progress")),
    )
    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _omp_tree_adapter(root)) == 1
    assert [(r.title, r.status) for r in await _rows(task_id)] == [
        ("Schritt 1 lesen", "done"),
        ("Schritt 2 rechnen", "in_progress"),
    ]


@pytest.mark.asyncio
async def test_task_runs_in_both_places_the_newest_plan_wins(tmp_path):
    """Mid-move: the first run's copy still in the folder, the retry run's
    copy already in ``.mc/`` — both are read, the newer plan counts."""
    t0 = _t0()
    _, _agent_id, task_id = await _scenario(dispatched_at=t0)
    root, _chat, _task_run = _live_case(tmp_path, task_id, t0, tag_header=True)
    _write(
        root / "--workspace--" / ".mc" / "2026-10-12T13-58-25_retry-sess.jsonl",
        _omp_header("retry-sess", t0 + timedelta(minutes=2, seconds=25), str(task_id)),
        _acp_plan("acp00048", t0 + timedelta(minutes=2, seconds=40), ("Schritt 1 lesen", "completed"), ("Schritt 2 rechnen", "completed")),
    )
    worker = sync.ChatTodoSync()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await worker.sync_once(s, adapter_for=lambda a: _omp_tree_adapter(root)) == 1
    assert [(r.title, r.status) for r in await _rows(task_id)] == [
        ("Schritt 1 lesen", "done"),
        ("Schritt 2 rechnen", "done"),
    ]


def test_session_globs_match_the_chat_reader():
    """The sync keeps its own glob tuple (it serves every harness), but it
    must look exactly where the chat reader looks. If ``.mc`` were renamed
    in ``omp_chat`` only, the sync would quietly stop finding task plans."""
    from app.services import omp_chat

    assert sync._SESSION_GLOBS == omp_chat._SESSION_GLOBS
    assert f"*/{omp_chat._MC_SINK_DIRNAME}/*.jsonl" in sync._SESSION_GLOBS
