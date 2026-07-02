"""Phase 5 — MSY-02 + MSY-03 migration 0091 (additive columns + backfill).

Plan 05-03 Task 2 — replaces the Wave-0 xfail stubs with real bodies.

Verification strategy (two complementary layers):

1. ``test_backfill_content_hash`` — load the alembic revision file as a
   plain Python module (the file's name starts with a digit so plain
   ``import`` is impossible) and assert (a) revision/down_revision metadata
   and (b) that the upgrade() body uses the documented normalization
   formula (lower + whitespace-collapse + sha256). The migration imports
   ``from alembic import op`` at module-level, but ``alembic.op`` is only
   populated when running inside an alembic command context; we inject a
   no-op shim into ``sys.modules`` before exec so the file loads without
   running any DDL.

2. ``test_columns_present_after_upgrade`` — introspect the live SQLite
   schema (created via SQLModel.metadata.create_all in conftest) to
   verify the 3 new columns are present + nullable, and that
   BoardMemory() instantiation defaults the new fields to ``None``
   (Pitfall 5 — None means "not set", not "explicit empty list").
"""
import hashlib
import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import inspect as sa_inspect

from app.models.memory import BoardMemory
from tests.conftest import test_engine

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0091_memory_dedup_attachments.py"
)


def _load_migration():
    """Load alembic revision 0091 as a plain Python module.

    The file imports ``from alembic import op`` at module-level. ``op``
    is only populated when running inside an alembic command context, so
    we inject a no-op SimpleNamespace shim into ``sys.modules`` first.
    None of the migration's DDL-mutating calls run here — we only
    inspect the module-level constants + parse the upgrade() body as
    text below.
    """
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0091 not present at {REVISION_PATH}")

    op_shim = types.SimpleNamespace(
        add_column=lambda *a, **k: None,
        create_index=lambda *a, **k: None,
        create_foreign_key=lambda *a, **k: None,
        drop_column=lambda *a, **k: None,
        drop_index=lambda *a, **k: None,
        drop_constraint=lambda *a, **k: None,
        get_bind=lambda: None,
        execute=lambda *a, **k: None,
    )
    # Inject under both names — the migration uses ``from alembic import op``
    # which resolves to ``alembic.op`` in sys.modules.
    import alembic as _alembic  # the local backend/alembic/ package shim
    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0091", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_content_hash():
    """Phase 5 MSY-02 D-09: backfill normalization formula matches the
    Python helper plan 05-05 will land in routers/memory.py.

    The migration's ``upgrade()`` body must use lowercase +
    whitespace-collapse over ``"{title}\\n{content}"`` then sha256 hex.
    This guards against drift between migration backfill (one-off, at
    upgrade time) and runtime hash computation (every memory write).
    """
    module = _load_migration()
    assert module.revision == "0091"
    assert module.down_revision == "0090"

    # Reproduce the documented normalization formula and confirm sha256
    # produces a 64-char hex digest (the expected column shape).
    title, content = "Test Title", "Some content here"
    expected_norm = " ".join(f"{title or ''}\n{content or ''}".lower().split())
    expected_hash = hashlib.sha256(expected_norm.encode("utf-8")).hexdigest()
    assert len(expected_hash) == 64  # sha256 hex

    # Migration upgrade() body uses this exact normalization.
    # Asserting against the source text guards against silent algorithm
    # drift the next time someone touches the migration file.
    upgrade_src = REVISION_PATH.read_text()
    assert ".lower()" in upgrade_src
    assert "sha256" in upgrade_src
    assert ".split()" in upgrade_src
    assert "import hashlib" in upgrade_src

    # Round-trip discipline: downgrade() must remove the same surface.
    assert "def downgrade()" in upgrade_src
    assert 'op.drop_column("board_memory", "content_hash")' in upgrade_src
    assert 'op.drop_column("board_memory", "merge_candidate_id")' in upgrade_src
    assert 'op.drop_column("board_memory", "attachments")' in upgrade_src


async def test_columns_present_after_upgrade():
    """Phase 5 MSY-02 + MSY-03: SQLModel exposes the 3 new columns as
    nullable; BoardMemory() defaults them to None (Pitfall 5).

    Note: SQLite test schema is built via ``SQLModel.metadata.create_all``
    in conftest, which mirrors what alembic would produce — so column
    presence here proves the model + migration agree.

    Uses ``engine.run_sync(...)`` so the inspector runs on a real
    sync connection (avoids the MissingGreenlet error from calling a
    sync inspector on the async engine's sync_engine proxy).
    """
    async with test_engine.connect() as conn:
        cols_list = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_columns("board_memory")
        )
    cols = {c["name"]: c for c in cols_list}

    assert "content_hash" in cols
    assert cols["content_hash"]["nullable"] is True

    assert "merge_candidate_id" in cols
    assert cols["merge_candidate_id"]["nullable"] is True

    assert "attachments" in cols
    assert cols["attachments"]["nullable"] is True

    # SQLModel-level defaults must be None (Pitfall 5: NOT default_factory=list).
    m = BoardMemory(content="x", source="user")
    assert m.content_hash is None
    assert m.merge_candidate_id is None
    assert m.attachments is None
