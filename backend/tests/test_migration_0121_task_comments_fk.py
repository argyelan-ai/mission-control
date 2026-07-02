"""Phase 28 Plan 28-01 — Verify migration 0121 source structure.

SQLite cannot execute ALTER TABLE DROP CONSTRAINT, and Alembic op is
only populated inside a real `alembic upgrade` context. This test
uses the same shim pattern as test_migration_0091.py: load the
migration as a plain module, then assert on revision metadata and
source text.
"""
from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import sys
import types

import pytest

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0121_task_comments_set_null_on_agent_fk.py"
)


def _load_migration():
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0121 not present at {REVISION_PATH}")

    op_shim = types.SimpleNamespace(
        drop_constraint=lambda *a, **k: None,
        create_foreign_key=lambda *a, **k: None,
    )
    # batch_alter_table returns a context manager that yields a
    # batch-op object exposing the same primitives.
    op_shim.batch_alter_table = lambda *a, **k: contextlib.nullcontext(op_shim)

    import alembic as _alembic
    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0121", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_metadata():
    """revision and down_revision wire 0121 onto 0120."""
    module = _load_migration()
    assert module.revision == "0121"
    assert module.down_revision == "0120"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_constraint_name_constant():
    """FK constant uses the canonical Postgres auto-name."""
    module = _load_migration()
    assert module._FK_NAME == "task_comments_author_agent_id_fkey"


def test_upgrade_sets_null_on_delete():
    """upgrade() declares ondelete='SET NULL' for the new FK."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert 'ondelete="SET NULL"' in upgrade_body, (
        "upgrade() must specify ondelete='SET NULL' for the FK swap"
    )


def test_downgrade_restores_strict_fk():
    """downgrade() recreates the FK without an ondelete clause."""
    src = REVISION_PATH.read_text()
    downgrade_body = src.split("def downgrade")[1]
    assert 'create_foreign_key' in downgrade_body
    # The downgrade must NOT include ondelete — that's the whole point.
    assert "ondelete" not in downgrade_body, (
        "downgrade() must restore the strict NO ACTION FK (no ondelete)"
    )


def test_no_fstring_sql():
    """STRIDE T-24-01: no f-string SQL interpolation anywhere."""
    src = REVISION_PATH.read_text()
    assert 'f"' not in src or src.count('f"') == src.count('f"""'), (
        "No f-string SQL allowed (T-24-01)"
    )
    assert "f'" not in src.replace("'", "")  # crude but effective


def test_uses_batch_alter_table_for_sqlite_compat():
    """batch_alter_table is required for SQLite test runners."""
    src = REVISION_PATH.read_text()
    assert src.count("batch_alter_table") == 2, (
        "Both upgrade() and downgrade() must use batch_alter_table"
    )


def test_upgrade_and_downgrade_callable():
    """Loading the migration with the shim does not raise."""
    module = _load_migration()
    # Both functions are no-ops under the shim, but must exist and
    # execute without error.
    module.upgrade()
    module.downgrade()
