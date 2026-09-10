"""Postgres lane (W0.2, Härtungsplan 10.09.2026): the guarantees the SQLite
suite can NEVER check.

1. The plpgsql trigger `validate_task_transition` (migration 0159) is the
   only transition guard enforced in production. Here it is exercised for the
   full matrix and must mirror `app.task_status.VALID_TRANSITIONS` 1:1.
2. Rex' review finding on PR #478 (B1): `select(...).with_for_update()` locks
   the ROW but does NOT refresh an object already in the session's identity
   map — a lock-and-validate helper that reads the status from such an object
   validates against a stale value (lost update). Only
   `populate_existing=True` makes the re-read fresh. Frische-Session-Tests
   never hit this; the trap needs ONE session + a concurrent writer.

Run: MC_TEST_DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test
     (schema via `alembic upgrade head`) — see .github/workflows/ci.yml.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.task import Task
from app.task_status import TaskStatus, VALID_TRANSITIONS
from tests.conftest import test_engine

pytestmark = pytest.mark.postgres

ALL_STATUSES = [s.value if hasattr(s, "value") else str(s) for s in VALID_TRANSITIONS] + [
    "user_test", "failed", "done", "aborted",
]
ALL_STATUSES = sorted(set(ALL_STATUSES))


async def _board_and_task(session: AsyncSession, status: str = "in_progress") -> Task:
    board = Board(name="PG", slug=f"pg-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    task = Task(board_id=board.id, title="pg probe", status=status)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _force_status(task_id: uuid.UUID, status: str) -> None:
    """Seed a row into an arbitrary status WITHOUT the trigger (replica
    role) — the trigger is what we test, so seeding must bypass it."""
    async with test_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(
            text("UPDATE tasks SET status = :s WHERE id = :id"),
            {"s": status, "id": task_id},
        )


async def test_trigger_exists():
    async with test_engine.connect() as conn:
        n = (await conn.execute(text(
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%task_transition%'"
        ))).scalar()
    assert n and n >= 1, "migration 0159 trigger missing — did `alembic upgrade head` run?"


async def test_trigger_rejects_waiting_to_done(session: AsyncSession):
    """The incident path of 10.09.: an operator PATCH waiting→done must be
    refused by the DATABASE, not only by the Python table."""
    task = await _board_and_task(session, "in_progress")
    await _force_status(task.id, "waiting")
    with pytest.raises(DBAPIError) as exc:
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'done' WHERE id = :id"), {"id": task.id}
            )
    assert "Invalid task transition" in str(exc.value)


async def test_trigger_mirrors_python_transition_table(session: AsyncSession):
    """Full matrix: every (from, to) the Python table allows must pass the
    trigger, every pair it forbids must be rejected. A drift in either
    direction is exactly the blind spot the SQLite lane has."""
    task = await _board_and_task(session, "in_progress")
    mismatches: list[str] = []
    for src in ALL_STATUSES:
        allowed = {str(getattr(v, "value", v)) for v in VALID_TRANSITIONS.get(src, set())}
        for dst in ALL_STATUSES:
            if src == dst:
                continue
            await _force_status(task.id, src)
            ok = True
            try:
                async with test_engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE tasks SET status = :d WHERE id = :id"),
                        {"d": dst, "id": task.id},
                    )
            except DBAPIError:
                ok = False
            if ok != (dst in allowed):
                mismatches.append(f"{src}->{dst}: trigger={'allow' if ok else 'reject'} python={'allow' if dst in allowed else 'reject'}")
    assert not mismatches, "trigger/table drift:\n" + "\n".join(mismatches)


async def test_with_for_update_does_not_refresh_identity_map(session: AsyncSession):
    """Rex B1 (PR #478): the lost-update trap. Session A already holds the
    task; a concurrent writer moves it to `done`; A's locked re-read still
    reports the OLD status unless populate_existing=True is used. Any
    lock-and-validate helper MUST use the fresh variant."""
    task = await _board_and_task(session, "in_progress")
    # Session A: object now lives in the identity map.
    async with AsyncSession(test_engine, expire_on_commit=False) as a:
        loaded = (await a.exec(select(Task).where(Task.id == task.id))).scalar_one()
        assert loaded.status == "in_progress"
        # Concurrent writer on its own connection (NullPool → real 2nd tx).
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'done' WHERE id = :id"), {"id": task.id}
            )
        stale = (await a.exec(
            select(Task).where(Task.id == task.id).with_for_update()
        )).scalar_one()
        assert stale.status == "in_progress", (
            "SQLAlchemy behaviour changed — the trap is gone; re-evaluate lock_and_set"
        )
        fresh = (await a.exec(
            select(Task).where(Task.id == task.id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one()
        assert fresh.status == "done"
        await a.rollback()
