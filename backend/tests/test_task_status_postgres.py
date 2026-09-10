"""Postgres lane (PR #478 nachgelegt, 10.09.2026): ``lock_and_set`` against
a real concurrent writer.

Rex' review of #478 (blocker B1) found that ``select(...).with_for_update()``
locks the ROW but does not refresh an object already sitting in the
session's identity map — a lock-and-validate helper that reads ``.status``
off such an object validates against a STALE value (lost update). The fix
is ``execution_options(populate_existing=True)`` (see
``app.services.task_state.lock_task``/``lock_and_set``). The SQLite suite
structurally cannot exercise this: SQLite does not implement
``FOR UPDATE``, and without a real second connection there is no race to
lose. This test needs two genuinely concurrent transactions against a real
Postgres.

PR #486 ("Backend Tests (Postgres lane)", W0.2) is the vehicle that will run
this — a CI job with a real ``postgres:16`` service, ``MC_TEST_DATABASE_URL``
switching the suite off SQLite, and a ``pytest.mark.postgres`` skip so this
lane's tests are never attempted on the default (SQLite) job. #486 is not
merged to main yet, so this file intentionally does NOT depend on any of
its conftest.py/pyproject.toml changes — it guards itself with the same
``MC_TEST_DATABASE_URL`` check inline, so it collects and is skipped
cleanly on every lane that exists today, and starts running for real the
moment this branch is updated onto a main that has #486. The Postgres-lane
rollup proof (job green) and the sabotage-probe red/green pair (remove
``populate_existing=True`` -> this test must fail) are therefore still
OUTSTANDING as of this commit — see the PR description.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.task import Task
from app.services.task_state import lock_and_set

_PG_TEST_URL = os.environ.get("MC_TEST_DATABASE_URL", "").strip()
POSTGRES_LANE = _PG_TEST_URL.startswith("postgresql")

pytestmark = pytest.mark.skipif(
    not POSTGRES_LANE,
    reason=(
        "needs a real Postgres (MC_TEST_DATABASE_URL) for two genuinely "
        "concurrent transactions — see PR #486 for the CI lane that provides it"
    ),
)

if POSTGRES_LANE:
    # NullPool: every AsyncSession gets its OWN connection, so the "session
    # A already holds the task" / "session B writes concurrently" split
    # below is two real transactions, not one connection lying to itself
    # (see task_state.py's module docstring and #486's identity-map test
    # for the same reasoning).
    from sqlalchemy.pool import NullPool
    _pg_engine = create_async_engine(_PG_TEST_URL, echo=False, poolclass=NullPool)
else:
    _pg_engine = None


async def _board_and_task(session: AsyncSession, status: str = "in_progress") -> Task:
    board = Board(name="PG lock_and_set", slug=f"pg-las-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    task = Task(board_id=board.id, title="lock_and_set probe", status=status)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.postgres
async def test_lock_and_set_sees_fresh_status_not_stale_identity_map(session: AsyncSession):
    """Same mechanic as #486's
    ``test_with_for_update_does_not_refresh_identity_map``, but exercising
    ``lock_and_set`` itself instead of a raw ``select()`` — this is the
    helper #478 actually ships, so it is this call that must prove the fix.

    Session A loads the task first (production case: every router that
    calls ``lock_and_set`` has already loaded the task once for its own
    ownership/404 guard, exactly like ``agent_task_status.py`` does before
    its ``lock_task()`` re-fetch). A concurrent writer on a second,
    independent connection then moves the task to ``done``. ``lock_and_set``
    in session A must see and validate against that fresh ``done`` — not
    the ``in_progress`` value already sitting in A's identity map — and
    must accept the transition ``done -> in_progress`` (the real,
    now-current status), not silently re-validate a transition from the
    stale one.
    """
    task = await _board_and_task(session, "in_progress")

    async with AsyncSession(_pg_engine, expire_on_commit=False) as a:
        loaded = (await a.exec(select(Task).where(Task.id == task.id))).scalar_one()
        assert loaded.status == "in_progress"

        # Concurrent writer: its own connection, its own transaction.
        async with _pg_engine.begin() as conn:
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
