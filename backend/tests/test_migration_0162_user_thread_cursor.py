"""Verify migration 0162 (user_thread_cursor) source structure.

Same shim pattern as test_migration_0091.py / test_migration_0121: load the
migration as a plain module with a stubbed alembic.op, then assert revision
metadata and the captured create_table/drop_table calls (Alembic op is only
populated inside a real `alembic upgrade` context).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0162_user_thread_cursor.py"
)


def _load_migration():
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0162 not present at {REVISION_PATH}")

    calls: dict[str, list] = {"create_table": [], "drop_table": []}
    op_shim = types.SimpleNamespace(
        create_table=lambda *a, **k: calls["create_table"].append((a, k)),
        drop_table=lambda *a, **k: calls["drop_table"].append((a, k)),
    )

    import alembic as _alembic
    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0162", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_migration_metadata():
    """revision/down_revision wire 0162 onto 0161."""
    module, _ = _load_migration()
    assert module.revision == "0162"
    assert module.down_revision == "0161"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_user_thread_cursor():
    """Composite PK (user_id, thread_id), mirroring agent_thread_cursor."""
    module, calls = _load_migration()
    module.upgrade()

    assert len(calls["create_table"]) == 1
    (table_name, *cols), _ = calls["create_table"][0]
    assert table_name == "user_thread_cursor"

    by_name = {c.name: c for c in cols}
    assert {name for name, c in by_name.items() if c.primary_key} == {"user_id", "thread_id"}
    assert "last_read_seq" in by_name
    assert "updated_at" in by_name

    # The thread FK cascades with its thread (same as agent_thread_cursor);
    # the user FK stays strict — cursor rows die with their container only.
    thread_fks = list(by_name["thread_id"].foreign_keys)
    assert len(thread_fks) == 1
    assert thread_fks[0].ondelete == "CASCADE"
    assert str(thread_fks[0].target_fullname) == "threads.id"
    user_fks = list(by_name["user_id"].foreign_keys)
    assert len(user_fks) == 1
    assert str(user_fks[0].target_fullname) == "users.id"


def test_downgrade_drops_table():
    module, calls = _load_migration()
    module.upgrade()
    module.downgrade()

    assert calls["drop_table"] == [(("user_thread_cursor",), {})]
