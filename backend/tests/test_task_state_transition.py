"""Tests for app.services.task_state.transition() — the single, row-locked
path for Task status changes (see task_state.py's module docstring).

The test engine is SQLite (see conftest.test_engine) which never takes the
``with_for_update()`` row lock (dialect guard, Postgres only — same as
routers/nodes.py:pair()). What IS testable here without a real Postgres is
the guard *logic*: transition() re-validates against the status it just
read, not a stale in-memory copy, so a second call that raced past the
first sees the already-updated row and gets rejected. This mirrors the
existing sequential-call race pattern used for
test_spawn_request_dedupe_by_name in test_boss_autonomy_phase2.py.
"""
import uuid

import pytest
from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.board import Board
from app.models.task import Task
from app.services.task_state import lock_and_set, transition
from tests.conftest import test_engine


async def _make_task(status: str = "inbox") -> uuid.UUID:
    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="B", slug=f"b-{uuid.uuid4().hex[:6]}"))
        s.add(Task(id=task_id, board_id=board_id, title="T", status=status))
        await s.commit()
    return task_id


async def _status_events(task_id: uuid.UUID) -> list[ActivityEvent]:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        result = await s.exec(
            select(ActivityEvent).where(
                ActivityEvent.task_id == task_id,
                ActivityEvent.event_type == "task.status_changed",
            )
        )
        return result.all()


@pytest.fixture(autouse=True)
def _wire_fake_redis(fake_redis):
    """emit_event() -> broadcast() -> get_redis() needs a live singleton;
    direct service calls (no ASGI client) don't go through the `client`
    fixture's dependency_overrides, so wire the module-level singleton
    the same way that fixture does in conftest.py."""
    import app.redis_client as redis_client_mod
    original = redis_client_mod._redis
    redis_client_mod._redis = fake_redis
    yield
    redis_client_mod._redis = original


@pytest.mark.asyncio
async def test_valid_transition_updates_status_and_fires_one_event():
    task_id = await _make_task(status="inbox")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await transition(s, task_id, "in_progress", actor="test", reason="ack")

    assert task.status == "in_progress"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        persisted = await s.get(Task, task_id)
        assert persisted.status == "in_progress"

    events = await _status_events(task_id)
    assert len(events) == 1, f"expected exactly one status_changed event, got {len(events)}"
    detail = events[0].detail
    assert detail["from"] == "inbox"
    assert detail["to"] == "in_progress"
    assert detail["actor"] == "test"
    assert detail["reason"] == "ack"


@pytest.mark.asyncio
async def test_invalid_transition_raises_409_and_leaves_status_unchanged():
    """done -> done isn't in VALID_TRANSITIONS (DONE: {IN_PROGRESS})."""
    task_id = await _make_task(status="done")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with pytest.raises(HTTPException) as exc_info:
            await transition(s, task_id, "done", actor="test", reason="noop")
        assert exc_info.value.status_code == 409

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        persisted = await s.get(Task, task_id)
        assert persisted.status == "done"

    assert await _status_events(task_id) == []


@pytest.mark.asyncio
async def test_unknown_task_raises_404():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with pytest.raises(HTTPException) as exc_info:
            await transition(s, uuid.uuid4(), "in_progress", actor="test", reason="x")
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_racing_transitions_one_wins_one_gets_409():
    """Two transitions land on the same task; only the first is valid by
    the time the second's (locked) read happens — the second must 409, not
    silently overwrite. On SQLite this is exercised sequentially (see
    module docstring); the lock this proves the *logic* for is what makes
    the same sequence safe under real concurrent Postgres writers."""
    task_id = await _make_task(status="review")

    async with AsyncSession(test_engine, expire_on_commit=False) as s1:
        first = await transition(s1, task_id, "done", actor="agent-a", reason="approved")
    assert first.status == "done"

    async with AsyncSession(test_engine, expire_on_commit=False) as s2:
        with pytest.raises(HTTPException) as exc_info:
            await transition(s2, task_id, "done", actor="agent-b", reason="approved-again")
        assert exc_info.value.status_code == 409

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        persisted = await s.get(Task, task_id)
        assert persisted.status == "done"

    events = await _status_events(task_id)
    assert len(events) == 1, (
        f"only the winning transition may fire an event, got {len(events)}"
    )
    assert events[0].detail["actor"] == "agent-a"


@pytest.mark.asyncio
async def test_lock_and_set_without_caller_commit_persists_nothing():
    """lock_and_set() must not commit itself — if the caller crashes or
    chooses not to commit, the in-memory status change never reaches the
    DB and no event fires. This is the contract transition() relies on
    (it commits explicitly right after calling lock_and_set())."""
    task_id = await _make_task(status="inbox")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task, from_status = await lock_and_set(s, task_id, "in_progress", actor="test")
        assert task.status == "in_progress"
        assert from_status == "inbox"
        # No commit here — session closes without persisting the change.

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        persisted = await s.get(Task, task_id)
        assert persisted.status == "inbox"

    assert await _status_events(task_id) == []


@pytest.mark.asyncio
async def test_dialect_guard_only_locks_on_postgresql():
    """SQLite (this test engine) must never hit with_for_update() — SQLite
    has no FOR UPDATE support and would raise. Asserting the dialect here
    documents the guard's precondition (session.bind.dialect.name check in
    task_state.transition) rather than re-testing SQLAlchemy itself."""
    task_id = await _make_task(status="inbox")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert s.bind.dialect.name == "sqlite"
        task = await transition(s, task_id, "in_progress", actor="test", reason="x")
        assert task.status == "in_progress"
