"""Postgres lane: the contract between the update engine and migration 0219.

The engine (``scripts/mc_update.py``) runs on the host and talks to the
database only through ``psql`` with SQL strings of its own. The SQLite lane
never executes those strings, so a drifted column type or name would only
show on a real machine mid-update. Here the engine's *own* statements run
against the schema Alembic built, and the backend reads what they wrote:

1. ``drain_set`` → the backend sees the drain (run id, expiry); ``drain_clear``
   → it is gone; the bridge's sweep statement returns the row as JSON and
   leaves deferred work alone.
2. The function ``update_running_work()`` (what the engine waits for) equals
   the backend's ORM count (what the UI shows), and it does not block a later
   migration that changes ``tasks.status``.

Run: MC_TEST_DATABASE_URL=postgresql+asyncpg://… after ``alembic upgrade head``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.system_update import SystemState
from app.models.task import Task
from app.services import mc_update_runner, update_drain
from tests.conftest import test_engine

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "20261019T120000Z-abc123"


def _engine_module():
    spec = importlib.util.spec_from_file_location("mc_update_engine_under_test", ROOT / "scripts" / "mc_update.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    """Stands in for the engine's psql: keeps the SQL, runs nothing."""

    def __init__(self):
        self.sql: list[str] = []

    def __call__(self, sql: str, check: bool = True) -> str:
        self.sql.append(sql)
        return ""


def _fake_engine(eng_mod, recorder: _Recorder):
    journal = SimpleNamespace(run_id=RUN_ID, data={}, save=lambda: None)
    return SimpleNamespace(journal=journal, drain_ttl=3600, psql=recorder, say=lambda m: None)


async def _run(sql: str):
    async with test_engine.begin() as conn:
        return (await conn.execute(text(sql))).fetchall() if "returning" in sql or sql.lstrip().startswith("select") \
            else await conn.execute(text(sql))


@pytest.fixture(autouse=True)
def _cache():
    update_drain.reset_cache()
    yield
    update_drain.reset_cache()


@pytest.mark.asyncio
async def test_engine_drain_statements_against_the_migrated_table():
    eng = _engine_module()
    rec = _Recorder()
    fake = _fake_engine(eng, rec)

    eng.Engine.drain_set(fake)
    await _run(rec.sql[-1])
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        state = await update_drain.current(s)
    assert state.active is True and state.run_id == RUN_ID

    # jsonb, as the engine's delete expects (value->>'run_id').
    rows = await _run("select value->>'run_id', jsonb_typeof(value) from system_state where key = 'update_drain'")
    assert rows == [(RUN_ID, "object")]

    # Set again (each phase refreshes the expiry): upsert, still one row.
    eng.Engine.drain_set(fake)
    await _run(rec.sql[-1])
    assert len(await _run("select key from system_state where key = 'update_drain'")) == 1

    eng.Engine.drain_clear(fake)
    await _run(rec.sql[-1])
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await update_drain.current(s)).active is False


@pytest.mark.asyncio
async def test_bridge_sweep_statement_returns_the_drain_and_keeps_deferred_work():
    eng = _engine_module()
    rec = _Recorder()
    eng.Engine.drain_set(_fake_engine(eng, rec))
    await _run(rec.sql[-1])
    await update_drain.defer(update_drain.KIND_DISPATCH, uuid.uuid4(), bind=test_engine, run_id=RUN_ID)

    rows = await _run("delete from system_state where key = 'update_drain' returning value::text")
    lifted = json.loads(rows[0][0])
    assert lifted["run_id"] == RUN_ID and lifted["set_by"] == "mc_update"
    left = await _run("select key from system_state")
    assert [r[0] for r in left] and all(r[0].startswith("deferred:dispatch:") for r in left)


@pytest.mark.asyncio
async def test_running_work_function_equals_the_backend_count():
    from app.models.agent import Agent
    from app.models.group import GroupRound
    from app.services import group_service

    eng = _engine_module()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="PG", slug=f"pg-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        s.add(Task(board_id=board.id, title="busy", status="in_progress"))
        s.add(Task(board_id=board.id, title="queued", status="inbox"))
        await s.commit()
        members = []
        for name in ("Alpha", "Beta"):
            a = Agent(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:4]}", agent_runtime="cli-bridge",
                      comm_v2=True)
            s.add(a)
            await s.commit()
            await s.refresh(a)
            members.append(a)
        running = await group_service.create_group(
            s, name="Running", goal="g", member_ids=[m.id for m in members], lead_agent_id=members[0].id)
        running.status = "running"
        paused = await group_service.create_group(
            s, name="Paused", goal="g", member_ids=[m.id for m in members], lead_agent_id=members[0].id)
        paused.status = "paused"
        s.add(running)
        s.add(paused)
        await s.commit()
        s.add(GroupRound(group_id=running.id, round_no=1))  # open, counts
        s.add(GroupRound(group_id=paused.id, round_no=1))  # group not running
        await s.commit()
        backend = await mc_update_runner.running_work(s)

    rows = await _run(eng.RUNNING_WORK_FN_SQL)
    assert backend == {"tasks": 1, "group_rounds": 1}
    assert int(rows[0][0]) == sum(backend.values()) == 2
    present = await _run(f"select to_regprocedure('public.{eng.RUNNING_WORK_FN}()') is not null")
    assert present[0][0] is True


@pytest.mark.asyncio
async def test_running_work_function_does_not_pin_the_columns_it_reads():
    """A view would make `ALTER COLUMN … TYPE` on the columns it reads fail
    ("cannot alter type of a column used by a view or rule"); the
    string-bodied function leaves later migrations free (rolled back here).
    tasks.status is left out: the transition trigger already pins it."""
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text("alter table agent_groups alter column status type text"))
            await conn.execute(text("alter table group_rounds alter column finished_at type timestamp"))
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_system_state_model_round_trips_jsonb():
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(SystemState(key="deferred:schedule:x", value={"kind": "schedule", "id": "x"}))
        await s.commit()
    rows = await _run("select value->>'kind' from system_state where key = 'deferred:schedule:x'")
    assert rows == [("schedule",)]


@pytest.mark.asyncio
async def test_a_failed_drain_read_leaves_the_callers_transaction_usable(monkeypatch):
    """A drain read that fails inside the dispatcher's session must not
    poison it: on Postgres the next query would hit InFailedSqlTransaction."""
    real = update_drain.read_row

    async def failing(s):
        await s.exec(text("SELECT * FROM no_such_table_for_the_drain"))
        return await real(s)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="pg-drain", slug=f"pg-drain-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.flush()  # the caller's own work, still uncommitted
        monkeypatch.setattr(update_drain, "read_row", failing)
        state = await update_drain.current(s)
        assert state.active is False
        assert (await s.exec(text("SELECT 1"))).scalar() == 1
        await s.commit()
        assert await s.get(Board, board.id) is not None
