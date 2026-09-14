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
   `test_with_for_update_does_not_refresh_identity_map` demonstrates the trap
   with a raw `select()`; `test_lock_and_set_sees_fresh_status_not_stale_identity_map`
   below exercises the actual production helper (`app.services.task_state.
   lock_and_set`) against the same trap — this is the sabotage-probe PR #478
   nachlegt: with `populate_existing=True` removed from `lock_and_set`, this
   second test must fail (see PR description for the red/green pair).

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
from app.services.task_state import lock_and_set
from app.task_status import TaskStatus, VALID_TRANSITIONS
from tests.conftest import test_engine


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


def test_lane_refuses_non_test_database_name():
    """Review #486 W1: the lane must never TRUNCATE a production-looking DB.
    Pure-Python guard, runs on both lanes."""
    from tests.conftest import _assert_test_database
    _assert_test_database("postgresql+asyncpg://u:p@localhost:5432/test")
    _assert_test_database("postgresql+asyncpg://u:p@localhost:5432/mc_test_lane")
    with pytest.raises(RuntimeError):
        _assert_test_database("postgresql+asyncpg://mc:pw@localhost:5432/mission_control")


@pytest.mark.postgres
async def test_trigger_exists():
    async with test_engine.connect() as conn:
        n = (await conn.execute(text(
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%task_transition%'"
        ))).scalar()
    assert n and n >= 1, "migration 0159 trigger missing — did `alembic upgrade head` run?"


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
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


@pytest.mark.postgres
async def test_lock_and_set_sees_fresh_status_not_stale_identity_map(session: AsyncSession):
    """Same mechanic as `test_with_for_update_does_not_refresh_identity_map`
    above, but exercising `lock_and_set` itself instead of a raw `select()`
    — this is the helper #478 actually ships, so it is this call that must
    prove the fix (the sabotage-probe target for this PR: remove
    `populate_existing=True` from `lock_and_set`/`lock_task` and this test
    must go red).

    Session A loads the task first (production case: every router that
    calls `lock_and_set` has already loaded the task once for its own
    ownership/404 guard, exactly like `agent_task_status.py` does before
    its `lock_task()` re-fetch). A concurrent writer on a second,
    independent connection then moves the task to `done`. `lock_and_set`
    in session A must see and validate against that fresh `done` — not
    the `in_progress` value already sitting in A's identity map — and
    must accept the transition `done -> in_progress` (the real,
    now-current status), not silently re-validate a transition from the
    stale one.
    """
    task = await _board_and_task(session, "in_progress")

    async with AsyncSession(test_engine, expire_on_commit=False) as a:
        loaded = (await a.exec(select(Task).where(Task.id == task.id))).scalar_one()
        assert loaded.status == "in_progress"

        # Concurrent writer: its own connection, its own transaction.
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'done' WHERE id = :id"),
                {"id": task.id},
            )

        # lock_and_set must validate against the FRESH ('done') status, not
        # the stale 'in_progress' copy still in A's identity map. 'done' ->
        # 'in_progress' is a valid transition; 'in_progress' -> 'in_progress'
        # (what a stale re-read would see, since `to` already equals it)
        # would not raise either way, so the assertion on `from_status`
        # below is what actually proves freshness, not just absence of a 409.
        locked, from_status = await lock_and_set(a, task.id, "in_progress", actor="test")

        assert from_status == "done", (
            "lock_and_set validated against a stale identity-map status "
            "instead of the concurrent writer's fresh value — populate_existing "
            "regression (PR #478 review, B1)"
        )
        assert locked.status == "in_progress"
        await a.rollback()


@pytest.mark.postgres
async def test_resume_task_after_answer_uses_lock_and_set_not_stale_status(
    session: AsyncSession,
):
    """`messaging.resume_task_after_answer()` merge-fix (2026-09-11, PR #478
    vs #496): #496 extracted the waiting→in_progress resume (previously
    inline in `routers/tasks.py`) into a shared helper so the LEAD answer
    path (`agent_scoped.py`, #496 review B4) gets the same resume as the
    operator path — but the extracted version wrote the status with a plain
    `task.status = TaskStatus.IN_PROGRESS` instead of going through
    `task_state.lock_and_set()`. Now that the helper runs from two
    concurrent-capable callers (operator + lead), a race between them needs
    the same fresh-read guard as any other transition, or the second caller
    silently double-resumes on a stale identity-map copy.

    Session A loads the task (identity map) while it is `waiting` — same as
    both call sites do before invoking this helper. A concurrent writer on
    an independent connection wins the race first and moves the task to
    `in_progress` (simulating the other caller's resume already landing).
    `resume_task_after_answer()` in session A must see that FRESH status,
    refuse the now-invalid in_progress→in_progress self-transition, and
    return False instead of posting a second "Antwort erhalten" line.

    Sabotage probe: swap `lock_and_set(...)` back for a plain `task.status =
    TaskStatus.IN_PROGRESS` assignment in `resume_task_after_answer` — this
    test goes red (returns True, posts a duplicate message) because the
    plain assignment validates against session A's stale `waiting` copy
    instead of the concurrent writer's fresh `in_progress`.
    """
    from app.models.agent import Agent
    from app.models.thread import Thread
    from app.services.messaging import open_questions, post_message, resume_task_after_answer

    board = Board(name="PG-resume", slug=f"pg-resume-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()

    agent = Agent(name="probe-agent", board_id=board.id, role="developer")
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    task = Task(
        board_id=board.id,
        title="resume race probe",
        status="waiting",
        assigned_agent_id=agent.id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    agent.current_task_id = task.id
    session.add(agent)
    await session.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as a:
        thread = Thread(kind="task", task_id=task.id)
        a.add(thread)
        await a.commit()
        await a.refresh(thread)

        # Session A: task object now lives in A's identity map as `waiting`.
        loaded = (await a.exec(select(Task).where(Task.id == task.id))).scalar_one()
        assert loaded.status == "waiting"

        # Concurrent writer on its own connection — the other caller's
        # resume_task_after_answer already won the race and moved the task
        # on. Bypass the trigger the way `_force_status` does: this models
        # an already-valid `waiting -> in_progress` transition that simply
        # happened on a different connection, not a malformed state.
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'in_progress' WHERE id = :id"),
                {"id": task.id},
            )

        remaining_before = await open_questions(a, thread_id=thread.id)
        assert remaining_before == []

        resumed = await resume_task_after_answer(a, loaded, thread, changed_by="test")

        assert resumed is False, (
            "resumed on a stale identity-map status instead of the "
            "concurrent writer's fresh 'in_progress' — lock_and_set() "
            "regression (PR #478 vs #496 merge)"
        )

        fresh = (await a.exec(
            select(Task).where(Task.id == task.id).execution_options(populate_existing=True)
        )).scalar_one()
        assert fresh.status == "in_progress"

        posted = await post_message(
            a, thread_id=thread.id, sender_type="system", body="probe: count check",
        )
        assert posted.seq == 1, (
            "resume_task_after_answer posted a message on the lost race — "
            f"expected the thread to still be empty, first real post got seq={posted.seq}"
        )
