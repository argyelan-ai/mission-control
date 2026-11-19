"""Migration 0229 — many chats per agent, run with real alembic operations.

SQLite lane: 0227 builds the record tables, rows go in, 0229 upgrades them
(existing agent chats become the default chats), then downgrades without
losing a row. The Postgres lane runs the whole chain in CI
(``alembic upgrade head``).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import uuid

import pytest
import sqlalchemy as sa

VERSIONS = pathlib.Path(__file__).parents[1] / "alembic" / "versions"


def _load(name: str):
    # The backend's own ``alembic/`` folder shadows the alembic package on
    # the test path (see test_bench_start_and_order.py): give the revision a
    # placeholder ``op`` at import time, the real one comes from ``_RealOp``.
    import alembic as _alembic

    placeholder = types.SimpleNamespace()
    _alembic.op = placeholder
    sys.modules["alembic.op"] = placeholder
    spec = importlib.util.spec_from_file_location(name, str(VERSIONS / f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RealOp:
    """The alembic ops 0227 and 0229 use, executed for real on SQLite."""

    def __init__(self, conn):
        self.conn = conn

    def get_bind(self):
        return self.conn

    def _table(self, name):
        meta = sa.MetaData()
        meta.reflect(self.conn)
        return meta, meta.tables.get(name)

    def create_table(self, name, *cols):
        meta, _ = self._table(name)
        sa.Table(name, meta, *cols).create(self.conn)

    def create_index(self, name, table, columns, unique=False, **kw):
        _, tbl = self._table(table)
        sa.Index(name, *[tbl.c[c] for c in columns], unique=unique, **kw).create(self.conn)

    def drop_index(self, name, table_name=None):
        self.conn.execute(sa.text(f"DROP INDEX {name}"))

    def add_column(self, table, column):
        from sqlalchemy.schema import CreateColumn

        ddl = CreateColumn(column).compile(dialect=self.conn.dialect)
        self.conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

    def drop_column(self, table, column):
        self.conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))

    def drop_table(self, name):
        self.conn.execute(sa.text(f"DROP TABLE {name}"))

    def execute(self, sql):
        self.conn.execute(sa.text(sql) if isinstance(sql, str) else sql)


def _run(conn, module, step: str) -> None:
    module.op = _RealOp(conn)
    getattr(module, step)()


def _indexes(conn) -> set[str]:
    return {i["name"] for i in sa.inspect(conn).get_indexes("chats")}


def test_upgrade_marks_existing_chats_default_and_downgrade_keeps_rows():
    engine = sa.create_engine("sqlite://")
    rev27, rev29 = _load("0227_chat_record"), _load("0229_chat_record_many_chats")
    assert rev29.down_revision == "0228_knowledge_root"
    agent, other = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        # 0227's server default is Postgres' NOW(); give SQLite one.
        conn.connection.driver_connection.create_function("NOW", 0, lambda: "2026-11-19 00:00:00")
        for t in ("users", "projects", "runtimes", "agents"):
            conn.execute(sa.text(f"CREATE TABLE {t} (id CHAR(32) PRIMARY KEY)"))
        _run(conn, rev27, "upgrade")
        conn.execute(sa.text(
            "INSERT INTO chats (id, title, legacy_agent_id, next_seq, pinned) VALUES "
            "(:a, 'Alpha', :agent, 1, 0), (:b, 'Loose', NULL, 1, 0)"
        ), {"a": uuid.uuid4().hex, "b": uuid.uuid4().hex, "agent": agent})

        _run(conn, rev29, "upgrade")
        rows = dict(conn.execute(sa.text("SELECT title, agent_default FROM chats")).all())
        assert rows == {"Alpha": 1, "Loose": 0}
        assert {"uq_chats_agent_default", "uq_chats_agent_native", "ix_chats_legacy_agent_id"} <= _indexes(conn)
        assert "uq_chats_legacy_agent" not in _indexes(conn)
        # A further chat on the same agent is allowed now …
        conn.execute(sa.text(
            "INSERT INTO chats (id, title, legacy_agent_id, next_seq, pinned, agent_default, native_session_id) "
            "VALUES (:c, 'Second', :agent, 1, 0, 0, 's-1')"
        ), {"c": uuid.uuid4().hex, "agent": agent})
        # … a second default chat or a second pin of the same session is not.
        for values in ((agent, 1, None), (agent, 0, "s-1")):
            with pytest.raises(sa.exc.IntegrityError):
                with conn.begin_nested():
                    conn.execute(sa.text(
                        "INSERT INTO chats (id, title, legacy_agent_id, next_seq, pinned, agent_default, "
                        "native_session_id) VALUES (:i, 'x', :agent, 1, 0, :d, :n)"
                    ), {"i": uuid.uuid4().hex, "agent": values[0], "d": values[1], "n": values[2]})
        # Another agent can pin the same native id.
        conn.execute(sa.text(
            "INSERT INTO chats (id, title, legacy_agent_id, next_seq, pinned, agent_default, native_session_id) "
            "VALUES (:c, 'Other', :agent, 1, 0, 0, 's-1')"
        ), {"c": uuid.uuid4().hex, "agent": other})

        _run(conn, rev29, "downgrade")
        rows = {
            t: (a, arch is not None)
            for t, a, arch in conn.execute(sa.text("SELECT title, legacy_agent_id, archived_at FROM chats")).all()
        }
        # Nothing deleted; the further chats are detached from their agent …
        assert set(rows) == {"Alpha", "Loose", "Second", "Other"}
        assert rows["Alpha"][0] == agent and rows["Second"][0] is None and rows["Other"][0] is None
        # … and archived: after the way back the importer reads their native
        # sessions into the agent's chat again, and a detached copy in the
        # list and search would show that history twice. A chat that never
        # had an agent is left alone.
        assert rows["Second"][1] and rows["Other"][1]
        assert not rows["Alpha"][1] and not rows["Loose"][1]
        assert "uq_chats_legacy_agent" in _indexes(conn)
        cols = {c["name"] for c in sa.inspect(conn).get_columns("chats")}
        assert "agent_default" not in cols and "native_session_id" not in cols
