"""A12 — mirror_path + heads_sync: every hop is a valid transition."""
from __future__ import annotations

import time
import uuid

import pytest
from sqlmodel import select

from app.models.task import Task, TaskComment, TaskEvent
from app.services.heads import mirror
from app.services.heads.mirror import HEAD_TO_TASK, mirror_path
from app.services.heads.sync import sync_once
from app.task_status import VALID_TRANSITIONS, TaskStatus, is_valid_transition
from tests.heads_backend_helpers import heads_root, iso, make_run, write_run_record  # noqa: F401

ALL = [str(s) for s in TaskStatus]


@pytest.mark.parametrize("head_state", sorted(HEAD_TO_TASK))
@pytest.mark.parametrize("from_status", ALL)
def test_every_head_state_from_every_status_is_a_valid_path(head_state, from_status):
    target = HEAD_TO_TASK[head_state]
    hops = mirror_path(from_status, target)
    if hops is None:
        # unreachable is only allowed where the transition table has no way out
        assert not VALID_TRANSITIONS.get(from_status)
        return
    cur = from_status
    for hop in hops:
        assert is_valid_transition(cur, hop), f"{cur} → {hop}"
        cur = hop
    assert cur == target


def test_spec_table_paths():
    assert mirror_path("inbox", "in_progress") == ["in_progress"]
    assert mirror_path("failed", "in_progress") == ["inbox", "in_progress"]
    assert mirror_path("waiting", "review") == ["in_progress", "review"]
    assert mirror_path("waiting", "failed") == ["blocked", "failed"]
    assert mirror_path("in_progress", "blocked") == ["blocked"]
    assert mirror_path("review", "review") == []


def test_stop_never_targets_aborted():
    assert HEAD_TO_TASK["stopped"] == "blocked"
    assert "aborted" not in HEAD_TO_TASK.values()
    assert HEAD_TO_TASK["passed"] == "review"  # never done — merge is the operator's


async def _task(session, make_board, make_task, **kw):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    return await make_task(board.id, status=kw.pop("status", "in_progress"), run_control="manual_hold", **kw)


async def test_sync_mirrors_passed_to_review_with_pr_and_events(session, heads_root, make_board, make_task):
    task = await _task(session, make_board, make_task)
    now = time.time()
    run_id = make_run(
        heads_root, task_id=str(task.id),
        status={"phase": "exited", "exit_code": 0, "pr_url": "https://github.com/o/r/pull/5",
                "started_at": iso(now - 600), "exited_at": iso(now - 5),
                "run_record_path": None},
    )
    rr = write_run_record(heads_root, run_id, mtime=now - 30)
    import json
    sp = heads_root / run_id / ".wrapper" / "status.json"
    st = json.loads(sp.read_text()); st["run_record_path"] = str(rr); sp.write_text(json.dumps(st))

    assert await sync_once(session) == 1
    fresh = await session.get(Task, task.id)
    await session.refresh(fresh)
    assert fresh.status == "review"
    assert fresh.pr_url == "https://github.com/o/r/pull/5"
    assert fresh.run_control == "manual_hold"
    events = (await session.exec(select(TaskEvent).where(TaskEvent.task_id == task.id))).all()
    assert [(e.from_status, e.to_status, e.actor_label) for e in events] == [("in_progress", "review", "head")]
    # mirrored once: the operator may move the card on, sync does not fight back
    fresh.status = "done"
    session.add(fresh)
    await session.commit()
    assert await sync_once(session) == 0
    await session.refresh(fresh)
    assert fresh.status == "done"


async def test_sync_needs_you_goes_to_waiting_with_blocker_comment(session, heads_root, make_board, make_task):
    task = await _task(session, make_board, make_task)
    make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "exit_code": 0},
             question="Remove the old endpoint? Recommend: deprecate.")
    await sync_once(session)
    task = await session.get(Task, task.id)
    await session.refresh(task)
    assert task.status == "waiting"
    comments = (await session.exec(select(TaskComment).where(TaskComment.task_id == task.id))).all()
    assert any(c.comment_type == "blocker" and "deprecate" in c.content for c in comments)


async def test_sync_stopped_goes_to_blocked_not_aborted(session, heads_root, make_board, make_task):
    task = await _task(session, make_board, make_task, status="waiting")
    make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "reason": "stopped"})
    await sync_once(session)
    task = await session.get(Task, task.id)
    await session.refresh(task)
    assert task.status == "blocked"


async def test_only_the_latest_run_of_a_task_is_mirrored(session, heads_root, make_board, make_task):
    """Restart: the old run exits 'stopped' while the new one runs — the card
    must stay in progress."""
    task = await _task(session, make_board, make_task)
    make_run(heads_root, task_id=str(task.id), created_ago=600, status={"phase": "exited", "reason": "stopped"})
    make_run(heads_root, task_id=str(task.id), created_ago=10, status={"phase": "running"}, heartbeat_age=5)
    await sync_once(session)
    task = await session.get(Task, task.id)
    await session.refresh(task)
    assert task.status == "in_progress"


async def test_deleted_task_writes_nothing_and_does_not_raise(session, heads_root):
    run_id = make_run(heads_root, task_id=str(uuid.uuid4()), status={"phase": "exited", "reason": "time_limit"})
    assert await sync_once(session) == 0
    import json
    mirror_file = json.loads((heads_root / run_id / ".backend" / "mirror.json").read_text())
    assert mirror_file["task_deleted"] is True


async def test_failed_from_waiting_goes_via_blocked(session, heads_root, make_board, make_task):
    task = await _task(session, make_board, make_task, status="waiting")
    make_run(heads_root, task_id=str(task.id), status={"phase": "exited", "reason": "time_limit"})
    await sync_once(session)
    events = (await session.exec(select(TaskEvent).where(TaskEvent.task_id == task.id))).all()
    assert [(e.from_status, e.to_status) for e in events] == [("waiting", "blocked"), ("blocked", "failed")]


def test_move_task_refuses_an_invalid_hop(monkeypatch):
    monkeypatch.setattr(mirror, "mirror_path", lambda a, b: ["aborted"])

    class T:
        status = "in_progress"
        id = uuid.uuid4()

    import asyncio

    assert asyncio.run(mirror.move_task(None, T(), "blocked", "x")) is False
