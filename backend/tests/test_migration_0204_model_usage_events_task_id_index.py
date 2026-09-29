"""Migration 0204 — index on model_usage_events(task_id).

The run-record endpoint (routers/run_record.py) filters model_usage_events
by task_id. The model declares ``Field(index=True)`` (fresh create_all
installs get ``ix_model_usage_events_task_id``), but migration 0127 never
created the index — migrated production DBs do a full scan over ~300k rows.

Verification strategy (same layer as test_migration_0201_expand.py):
load the revision file as a plain module (shim-injected ``alembic.op``),
bind a real op to a live SQLite connection, run upgrade()/downgrade() and
introspect the actual indexes.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

VERSIONS_DIR = pathlib.Path(__file__).parents[1] / "alembic" / "versions"
INDEX_NAME = "ix_model_usage_events_task_id"


def _load_revision(name: str):
    """Load a migration file as a plain module. Same shim-injection pattern
    as test_migration_0201_expand.py."""
    path = VERSIONS_DIR / f"{name}.py"
    if not path.is_file():
        pytest.fail(f"Migration {name} not present at {path}")

    op_placeholder = types.SimpleNamespace()
    import alembic as _alembic
    _alembic.op = op_placeholder
    sys.modules["alembic.op"] = op_placeholder

    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RealIndexOp:
    """Binds alembic's op.create_index/drop_index to a real sync connection."""

    def __init__(self, conn):
        self.conn = conn

    def create_index(self, index_name, table, columns, **_kwargs):
        cols = ", ".join(columns)
        self.conn.execute(text(f"CREATE INDEX {index_name} ON {table} ({cols})"))

    def drop_index(self, index_name, table=None, **_kwargs):
        self.conn.execute(text(f"DROP INDEX {index_name}"))


def _run(module, direction, real_op):
    """Bind real_op as the module's op (the module-level `from alembic import
    op` captured the load-time placeholder) and run upgrade()/downgrade()."""
    module.op = real_op
    getattr(module, direction)()


async def _get_index_names(engine, table_name):
    async with engine.connect() as conn:
        return {
            ix["name"]
            for ix in await conn.run_sync(
                lambda sc: sa_inspect(sc).get_indexes(table_name)
            )
        }


async def test_0204_creates_and_drops_task_id_index(tmp_path):
    module = _load_revision("0204_model_usage_events_task_id_index")
    assert module.revision == "0204_model_usage_task_id"
    assert module.down_revision == "0203_claude5_model_prices"

    db_path = tmp_path / "index_proof.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    # ── scratch table shaped like migration 0127's model_usage_events:
    #    task_id present, NO task_id index (today's migrated production state) ──
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE model_usage_events (
                    id CHAR(32) PRIMARY KEY,
                    task_id CHAR(32),
                    agent_id CHAR(32),
                    model VARCHAR NOT NULL,
                    ts TIMESTAMP NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text("CREATE INDEX ix_model_usage_events_agent_id ON model_usage_events (agent_id)")
        )

    before = await _get_index_names(engine, "model_usage_events")
    assert INDEX_NAME not in before, "index must be missing before upgrade"

    # ── upgrade: index appears ──
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: _run(module, "upgrade", _RealIndexOp(sc)))

    after = await _get_index_names(engine, "model_usage_events")
    assert INDEX_NAME in after, f"upgrade must create {INDEX_NAME}, got {sorted(after)}"

    # ── downgrade: index gone again ──
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: _run(module, "downgrade", _RealIndexOp(sc)))

    final = await _get_index_names(engine, "model_usage_events")
    assert INDEX_NAME not in final, "downgrade must drop the index"
    # unrelated index untouched
    assert "ix_model_usage_events_agent_id" in final

    await engine.dispose()
