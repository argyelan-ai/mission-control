"""Tests for the Task Flight Recorder — GET .../tasks/{task_id}/timeline.

Correlates existing event sources (TaskEvent, ActivityEvent, TaskComment,
Task field milestones) into one chronological list. No new event capture —
this endpoint only reads and merges what already exists.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_timeline_merges_sources_in_chronological_order(auth_client, make_board, make_task):
    """Milestones, TaskEvents, ActivityEvents and TaskComments all show up,
    sorted ascending by timestamp."""
    board = await make_board()
    base = datetime(2026, 7, 1, 12, 0, 0)
    task = await make_task(
        board.id,
        status="in_progress",
        created_at=base,
        dispatched_at=base + timedelta(minutes=1),
        ack_at=base + timedelta(minutes=2),
    )

    from app.models.task import TaskEvent, TaskComment
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(
            task_id=task.id, from_status="inbox", to_status="in_progress",
            changed_by="agent", reason="ack", created_at=base + timedelta(minutes=3),
        ))
        s.add(ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.recovery_triggered",
            title="Recovery triggered", created_at=base + timedelta(minutes=4),
        ))
        s.add(TaskComment(
            task_id=task.id, author_type="agent", comment_type="progress",
            content="Wrote the endpoint, running tests now.",
            created_at=base + timedelta(minutes=5),
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["task"]["id"] == str(task.id)
    assert body["truncated"] is False

    entries = body["entries"]
    timestamps = [e["ts"] for e in entries]
    assert timestamps == sorted(timestamps)

    kinds = [e["kind"] for e in entries]
    assert "created" in kinds
    assert "dispatched" in kinds
    assert "acked" in kinds
    assert "status_change" in kinds
    assert "recovery" in kinds
    assert "progress" in kinds

    progress_entry = next(e for e in entries if e["kind"] == "progress")
    assert progress_entry["source"] == "comment"
    assert "Wrote the endpoint" in progress_entry["detail"]

    recovery_entry = next(e for e in entries if e["kind"] == "recovery")
    assert recovery_entry["source"] == "activity_event"

    created_entry = next(e for e in entries if e["kind"] == "created")
    assert created_entry["source"] == "milestone"


@pytest.mark.asyncio
async def test_timeline_long_comment_is_truncated(auth_client, make_board, make_task):
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", comment_type="reflection",
            content="x" * 1000,
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    reflection_entry = next(e for e in entries if e["kind"] == "reflection")
    assert len(reflection_entry["detail"]) <= 320  # ~300 chars + ellipsis marker
    assert reflection_entry["detail"] != "x" * 1000


@pytest.mark.asyncio
async def test_timeline_no_events_returns_only_created_milestone(auth_client, make_board, make_task):
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "created"


@pytest.mark.asyncio
async def test_timeline_404_for_wrong_board(auth_client, make_board, make_task):
    board = await make_board()
    other_board = await make_board(name="Other Board", slug="other-board")
    task = await make_task(board.id, status="inbox")

    resp = await auth_client.get(
        f"/api/v1/boards/{other_board.id}/tasks/{task.id}/timeline"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_timeline_404_for_missing_task(auth_client, make_board):
    board = await make_board()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{uuid.uuid4()}/timeline"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_timeline_requires_auth(client, make_board, make_task):
    """Same auth as the neighboring GET .../events endpoint (no headers = 401)."""
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    resp = await client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_timeline_caps_at_500_and_keeps_most_recent(auth_client, make_board, make_task):
    board = await make_board()
    base = datetime(2026, 1, 1, 0, 0, 0)
    task = await make_task(board.id, status="inbox", created_at=base)

    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for i in range(520):
            s.add(TaskComment(
                task_id=task.id, author_type="agent", comment_type="progress",
                content=f"update {i}",
                created_at=base + timedelta(minutes=i + 1),
            ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["entries"]) == 500
    # oldest kept entry should be the "created" milestone is dropped since
    # the cap keeps the most RECENT 500 — the created milestone (oldest of
    # all) is the first one to be cut.
    kinds = [e["kind"] for e in body["entries"]]
    assert "created" not in kinds


@pytest.mark.asyncio
async def test_timeline_status_change_agent_actor_uses_agent_name(auth_client, make_board, make_task, make_agent):
    board = await make_board()
    agent = await make_agent(board_id=board.id, name="Cody")
    task = await make_task(board.id, status="in_progress")

    from app.models.task import TaskEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(
            task_id=task.id, from_status="inbox", to_status="in_progress",
            changed_by="agent", agent_id=agent.id,
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    entries = resp.json()["entries"]
    status_entry = next(e for e in entries if e["kind"] == "status_change")
    assert status_entry["actor"] == "Cody"


# ── MEDIUM 1 — auto-promote transitions must not be swallowed ────────────


@pytest.mark.asyncio
async def test_timeline_keeps_status_changed_activity_event_without_matching_task_event(
    auth_client, make_board, make_task, make_agent,
):
    """Resolution auto-promote (agent_comments.py) emits ONLY an ActivityEvent
    task.status_changed — it never calls record_task_event(). Blindly
    skipping task.status_changed would silently drop this transition from
    the timeline. It must show up, with kind="status_change" (same as a
    TaskEvent-sourced transition) so it renders identically."""
    board = await make_board()
    agent = await make_agent(board_id=board.id, name="Rex")
    task = await make_task(board.id, status="review")

    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(
            task_id=task.id, board_id=board.id, agent_id=agent.id,
            event_type="task.status_changed",
            title="Auto-Promote: Rex resolution-Kommentar → review",
            detail={"old_status": "in_progress", "new_status": "review", "auto_promoted": True},
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    entries = resp.json()["entries"]
    status_entries = [e for e in entries if e["kind"] == "status_change"]
    assert len(status_entries) == 1
    entry = status_entries[0]
    assert entry["source"] == "activity_event"
    assert entry["title"] == "in progress → review"
    assert entry["actor"] == "Rex"


@pytest.mark.asyncio
async def test_timeline_skips_status_changed_activity_event_with_matching_task_event(
    auth_client, make_board, make_task,
):
    """The common path (e.g. the PATCH endpoint) writes a TaskEvent AND emits
    a task.status_changed ActivityEvent for the same transition within
    milliseconds — that ActivityEvent IS a duplicate and must be skipped, or
    every ordinary status change would show up twice."""
    board = await make_board()
    task = await make_task(board.id, status="in_progress")
    now = datetime(2026, 7, 10, 9, 0, 0)

    from app.models.task import TaskEvent
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskEvent(
            task_id=task.id, from_status="inbox", to_status="in_progress",
            changed_by="user", created_at=now,
        ))
        s.add(ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.status_changed",
            title="Status changed", detail={"old_status": "inbox", "new_status": "in_progress"},
            created_at=now + timedelta(seconds=1),
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    entries = resp.json()["entries"]
    status_entries = [e for e in entries if e["kind"] == "status_change"]
    assert len(status_entries) == 1
    assert status_entries[0]["source"] == "task_event"


# ── MEDIUM 2 — ActivityEvent.detail must reach the timeline ──────────────


@pytest.mark.asyncio
async def test_timeline_activity_event_detail_is_shown(auth_client, make_board, make_task):
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.dispatch_failed",
            title="Dispatch failed", severity="error",
            detail={"reason": "docker timeout", "attempt": 3},
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    entries = resp.json()["entries"]
    dispatch_entry = next(e for e in entries if e["kind"] == "dispatch")
    assert "reason: docker timeout" in dispatch_entry["detail"]
    assert dispatch_entry["meta"]["detail"] == {"reason": "docker timeout", "attempt": 3}


# ── Small fixes ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeline_stable_sort_for_identical_timestamps(auth_client, make_board, make_task):
    """Two comments at the exact same timestamp must sort deterministically
    by id, not by incidental insertion/DB-read order."""
    board = await make_board()
    task = await make_task(board.id, status="inbox")
    same_ts = datetime(2026, 7, 10, 10, 0, 0)

    id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
    id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")

    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Insert in reverse id order — the sort itself must impose id order.
        s.add(TaskComment(
            id=id_b, task_id=task.id, author_type="agent", comment_type="progress",
            content="second", created_at=same_ts,
        ))
        s.add(TaskComment(
            id=id_a, task_id=task.id, author_type="agent", comment_type="progress",
            content="first", created_at=same_ts,
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    entries = resp.json()["entries"]
    progress_entries = [e for e in entries if e["kind"] == "progress"]
    assert [e["detail"] for e in progress_entries] == ["first", "second"]


@pytest.mark.asyncio
async def test_timeline_comment_not_duplicated_by_its_activity_event(auth_client, make_board, make_task):
    """Posting a comment also emits a task.commented ActivityEvent (see
    add_comment() in this router) — the timeline must show the comment once,
    not twice."""
    board = await make_board()
    task = await make_task(board.id, status="inbox")

    from app.models.task import TaskComment
    from app.models.activity import ActivityEvent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task.id, author_type="agent", comment_type="progress",
            content="Wrote the endpoint.",
        ))
        s.add(ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.commented",
            title="Comment on Test Task",
        ))
        await s.commit()

    resp = await auth_client.get(
        f"/api/v1/boards/{board.id}/tasks/{task.id}/timeline"
    )
    entries = resp.json()["entries"]
    matching = [e for e in entries if "Wrote the endpoint" in (e.get("detail") or "") or e["kind"] == "progress"]
    assert len(matching) == 1
    assert matching[0]["source"] == "comment"
