"""Tests for Task 11 — Agent.comm_v2 pilot flag + thread backfill migration.

Two things under test:
  (a) Agent.comm_v2 defaults to False (the pilot gate everything else in
      Interaction Model 2.0 hangs off of — see routers/agents.py,
      agent_comments.py, task_runner.py).
  (b) The backfill: every pre-existing Task without a thread_id gets a
      Thread(kind="task") + a seed system Message at seq 1, so
      last_message_at is never NULL for a task that predates threads
      (§10.4). Exercised via two callables:
        - app.services.messaging.backfill_task_threads (async/ORM) — the
          one this suite drives directly.
        - alembic revision 0160's own backfill_task_threads(conn) (sync/
          plain-SQL) — the one the real `alembic upgrade` runs. Loaded as a
          plain module (same shim pattern as test_migration_0091.py) and
          run against the same SQLite connection the async ORM step used,
          to prove the two implementations agree.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.task import Task
from app.models.thread import Message, Thread
from app.services.messaging import BACKFILL_SEED_BODY, backfill_task_threads

from tests.conftest import test_engine

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0160_agent_comm_v2_and_thread_backfill.py"
)


def _load_migration():
    """Load alembic revision 0160 as a plain module (op is only populated
    inside a real alembic command context; backfill_task_threads never
    touches op, only the connection it's handed, so a bare shim suffices)."""
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0160 not present at {REVISION_PATH}")

    op_shim = types.SimpleNamespace(
        add_column=lambda *a, **k: None,
        get_bind=lambda: None,
        drop_column=lambda *a, **k: None,
    )
    import alembic as _alembic
    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0160", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
class TestCommV2Default:
    async def test_agent_default_comm_v2_false(self, make_agent):
        agent = await make_agent("Pilot Candidate")
        assert agent.comm_v2 is False

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            reloaded = await s.get(Agent, agent.id)
            assert reloaded.comm_v2 is False


@pytest.mark.asyncio
class TestBackfillTaskThreadsAsync:
    async def test_task_without_thread_gets_thread_and_seed_message(
        self, make_board, make_task
    ):
        board = await make_board()
        task = await make_task(board.id, title="Pre-comm_v2 task")
        assert task.thread_id is None

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            count = await backfill_task_threads(s)
        assert count == 1

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            reloaded = await s.get(Task, task.id)
            assert reloaded.thread_id is not None

            thread = await s.get(Thread, reloaded.thread_id)
            assert thread.kind == "task"
            assert thread.task_id == task.id

            result = await s.exec(
                select(Message).where(Message.thread_id == thread.id)
            )
            messages = result.all()
            assert len(messages) == 1
            assert messages[0].seq == 1
            assert messages[0].sender_type == "system"
            assert messages[0].message_type == "system"
            assert messages[0].body == BACKFILL_SEED_BODY

    async def test_idempotent_second_run_creates_nothing(self, make_board, make_task):
        board = await make_board()
        task = await make_task(board.id, title="Pre-comm_v2 task")

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            first_count = await backfill_task_threads(s)
        assert first_count == 1

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            second_count = await backfill_task_threads(s)
        assert second_count == 0

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await s.exec(select(Thread))
            threads = result.all()
            result = await s.exec(select(Message))
            messages = result.all()
        assert len(threads) == 1
        assert len(messages) == 1

    async def test_task_with_existing_thread_is_untouched(self, make_board, make_task):
        board = await make_board()
        task = await make_task(board.id, title="Already-threaded task")

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            existing_thread = Thread(kind="task", task_id=task.id)
            s.add(existing_thread)
            await s.commit()
            await s.refresh(existing_thread)

            db_task = await s.get(Task, task.id)
            db_task.thread_id = existing_thread.id
            s.add(db_task)
            await s.commit()

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            count = await backfill_task_threads(s)
        assert count == 0

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await s.exec(select(Message))
            messages = result.all()
        assert messages == []


@pytest.mark.asyncio
class TestBackfillTaskThreadsMigrationSync:
    """The real `alembic upgrade` runs revision 0160's own sync/plain-SQL
    backfill_task_threads(conn), not the async ORM one above. Prove the two
    agree by running the migration's version against the same SQLite schema,
    over the sync Connection wrapped by AsyncConnection.run_sync."""

    async def test_sync_variant_matches_async_variant(self, make_board, make_task):
        board = await make_board()
        task = await make_task(board.id, title="Pre-comm_v2 task")

        module = _load_migration()

        async with test_engine.begin() as conn:
            count = await conn.run_sync(lambda sync_conn: module.backfill_task_threads(sync_conn))
        assert count == 1

        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            reloaded = await s.get(Task, task.id)
            assert reloaded.thread_id is not None

            result = await s.exec(
                select(Message).where(Message.thread_id == reloaded.thread_id)
            )
            messages = result.all()
            assert len(messages) == 1
            assert messages[0].seq == 1
            assert messages[0].body == module.SEED_MESSAGE_BODY

        # idempotent here too
        async with test_engine.begin() as conn:
            second_count = await conn.run_sync(
                lambda sync_conn: module.backfill_task_threads(sync_conn)
            )
        assert second_count == 0
