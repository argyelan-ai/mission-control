"""Test migration 0123 (Phase 30 — drop gateway schema + add discord_config).

Mirrors test_migration_0122_henry_sunset.py. Two test layers:
  - Source-text invariants (regex against the migration file) — fast.
  - SQLite E2E (live upgrade() against the in-memory test_engine) — slower.

Per Phase 28 LIVE-RUN lesson #1, this test file proves both the *structure*
of the migration (Pre-Flight before mutation, PyUUID bindparams, no try/except,
no f-string SQL, workspace_path preserved) AND the *behavior* of running it
end-to-end against SQLite (gateways table gone, discord_config seeded, inert
agents deleted, normal agent intact, gateway_* columns dropped).
"""
from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import sys
import types
import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlmodel.ext.asyncio.session import AsyncSession

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0123_drop_gateway_schema_add_discord_config.py"
)


def _load_migration():
    """Load migration 0123 as a plain module with alembic.op shimmed.

    Shim surface: get_bind + execute + create_table + drop_table +
    add_column + drop_column + create_check_constraint + drop_constraint +
    batch_alter_table (returns context manager yielding a shim with
    drop_column + alter_column). Mirrors the shim in
    test_migration_0122_henry_sunset.py + test_migration_0121_task_comments_fk.py.
    """
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0123 not present at {REVISION_PATH}")

    class _MockResult:
        def fetchone(self):
            # Pre-Flight reads a count — return (0,) so the upgrade body
            # proceeds past the RuntimeError gate. Inert-agent ID lookup
            # returns [] via fetchall (next call returns same _MockResult
            # but only fetchall is used).
            return (0,)

        def fetchall(self):
            return []

    mock_bind = types.SimpleNamespace(execute=lambda *a, **k: _MockResult())
    op_shim = types.SimpleNamespace(
        get_bind=lambda: mock_bind,
        execute=lambda *a, **k: None,
        create_table=lambda *a, **k: None,
        drop_table=lambda *a, **k: None,
        add_column=lambda *a, **k: None,
        drop_column=lambda *a, **k: None,
        alter_column=lambda *a, **k: None,
        create_check_constraint=lambda *a, **k: None,
        drop_constraint=lambda *a, **k: None,
    )
    # batch_alter_table returns a context manager yielding a batch-op
    # object that exposes drop_column + alter_column.
    op_shim.batch_alter_table = lambda *a, **k: contextlib.nullcontext(op_shim)

    import alembic as _alembic

    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0123", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===== Source-text invariants (no DB needed) ============================


def test_migration_metadata():
    """revision/down_revision wire 0123 onto 0122."""
    module = _load_migration()
    assert module.revision == "0123"
    assert module.down_revision == "0122"


def test_pre_flight_check_present():
    """upgrade() raises RuntimeError BEFORE any mutation (CONTEXT.md D-09)."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert "RuntimeError" in upgrade_body
    assert "openclaw" in upgrade_body
    # Pre-Flight must run BEFORE any DROP/DELETE.
    pre_flight_idx = upgrade_body.find("RuntimeError")
    first_drop_idx = upgrade_body.find("op.drop")
    first_delete_idx = upgrade_body.find("DELETE FROM")
    first_create_table_idx = upgrade_body.find("op.create_table")
    assert pre_flight_idx > 0
    if first_drop_idx > 0:
        assert pre_flight_idx < first_drop_idx
    if first_delete_idx > 0:
        assert pre_flight_idx < first_delete_idx
    if first_create_table_idx > 0:
        assert pre_flight_idx < first_create_table_idx


def test_inert_test_agents_constant():
    """The two known inert openclaw test agents are named constants (D-10)."""
    module = _load_migration()
    assert module.INERT_TEST_NAMES == ("Bug18Test", "D2Test")


def test_uses_pyuuid_for_bindparams():
    """All id bindings convert to _PyUUID before bind.execute (D-04, Phase 28 lesson)."""
    src = REVISION_PATH.read_text()
    assert "_PyUUID" in src
    assert "from uuid import UUID as _PyUUID" in src


def test_no_tryexcept_around_sql():
    """Phase 28 LIVE-RUN lesson: try/except does not roll back Postgres aborts."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert "try:" not in upgrade_body, (
        "Phase 28 LIVE-RUN lesson: try/except around SQL is forbidden — one "
        "failure aborts the whole tx regardless of the Python catch. Use "
        "Pre-Flight checks instead."
    )


def test_drop_column_order_is_fk_safe():
    """gateway_id columns are dropped BEFORE gateways table (D-12).

    Strips comment lines first so the test isn't fooled by literal
    `drop_column(...)` / `drop_table(...)` substrings appearing inside
    explanatory comments.
    """
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    # Strip comment-only content (lines whose first non-whitespace char is #).
    code_lines = [
        l for l in upgrade_body.splitlines()
        if not l.lstrip().startswith("#")
    ]
    code_body = "\n".join(code_lines)
    drop_col_idx = code_body.find('drop_column("gateway_id")')
    drop_table_idx = code_body.find('drop_table("gateways")')
    assert drop_col_idx > 0, "Must have drop_column('gateway_id') in code"
    assert drop_table_idx > 0, "Must have drop_table('gateways') in code"
    assert drop_col_idx < drop_table_idx, (
        "drop_column('gateway_id') must precede drop_table('gateways') in code"
    )


def test_workspace_path_not_dropped():
    """workspace_path KEPT per Plan 30-01/02 scope clarification (CONTEXT.md D-12)."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert 'drop_column("workspace_path")' not in upgrade_body, (
        "agents.workspace_path is in active use by cli-bridge + host runtime "
        "(10/14 agents have values, 4 active consumers: agent_git.py, "
        "agent_scoped.py, dispatch.py, tasks.py). Dropping it breaks the fleet."
    )


def test_discord_config_table_created():
    """Step 3 — discord_config table with expected columns (CONTEXT.md D-05)."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert '"discord_config"' in upgrade_body
    assert "guild_id" in upgrade_body
    assert "category_id" in upgrade_body
    assert "bot_configured" in upgrade_body


def test_discord_config_seeded_from_gateways():
    """Step 4 — INSERT INTO discord_config SELECT FROM gateways (D-07)."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert "INSERT INTO discord_config" in upgrade_body
    assert "FROM gateways" in upgrade_body


def test_check_constraint_excludes_openclaw():
    """Step 7 — CHECK constraint on agent_runtime forbids 'openclaw' (D-11 reinterpretation)."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert "create_check_constraint" in upgrade_body
    assert "ck_agents_agent_runtime_not_openclaw" in upgrade_body
    assert "agent_runtime" in upgrade_body
    # The allowed-list line itself must NOT contain 'openclaw'.
    check_line = [l for l in upgrade_body.splitlines() if "agent_runtime IN" in l]
    assert check_line, "Must declare allowed-runtime CHECK"
    assert "'openclaw'" not in check_line[0]


def test_no_enum_type_swap():
    """D-11 corrected: agent_runtime is plain TEXT, no enum-type-swap workaround."""
    src = REVISION_PATH.read_text()
    # Common enum-DDL idioms that would indicate someone misread D-11.
    assert "CREATE TYPE" not in src.upper(), (
        "agent_runtime is character varying (migration 0031:19), not a "
        "Postgres ENUM. Use create_check_constraint instead of enum-type-swap."
    )
    assert "ALTER TYPE" not in src.upper()
    assert "DROP TYPE" not in src.upper()


def test_no_fstring_sql_interpolation():
    """STRIDE T-30-03-01: no f-string SQL (injection vector)."""
    src = REVISION_PATH.read_text()
    for keyword in ("UPDATE", "DELETE", "INSERT", "SELECT"):
        assert f'f"{keyword}' not in src, (
            f"f-string SQL forbidden — found f-string with leading {keyword!r}. "
            f"Use sa.text(...).bindparams(...) for parameterized queries."
        )
        assert f"f'{keyword}" not in src


def test_downgrade_recreates_gateways():
    """Best-effort downgrade per D-14 — restore gateways table + copy back."""
    src = REVISION_PATH.read_text()
    downgrade_body = src.split("def downgrade")[1]
    assert '"gateways"' in downgrade_body
    assert "discord_guild_id" in downgrade_body
    assert "INSERT INTO gateways" in downgrade_body
    assert "drop_constraint" in downgrade_body
    assert "ck_agents_agent_runtime_not_openclaw" in downgrade_body


def test_upgrade_callable_with_shim():
    """Migration body executes against no-op shim without error (smoke test)."""
    module = _load_migration()
    module.upgrade()


# ===== SQLite E2E (live test_engine) ====================================


class _SqliteOpShim:
    """Translates a small subset of alembic.op DDL calls to SQLite-native SQL.

    The local `./alembic/__init__.py` (migration versions dir) shadows the
    installed alembic package when tests run from the backend/ working dir,
    so we cannot `from alembic.operations import Operations` here. Instead
    we hand-roll the 6 op calls migration 0123 actually uses:

      op.create_table        → CREATE TABLE
      op.drop_table          → DROP TABLE
      op.add_column          → ALTER TABLE ADD COLUMN (downgrade only)
      op.create_check_constraint → no-op (SQLite doesn't enforce
                                   table-level CHECK retroactively)
      op.drop_constraint     → no-op
      op.batch_alter_table   → context manager exposing drop_column +
                               alter_column on the wrapped table

    SQLite 3.35+ supports ALTER TABLE DROP COLUMN natively (verified via
    sqlite3.sqlite_version on this machine: 3.51.x).
    """

    def __init__(self, sync_conn):
        self._conn = sync_conn
        self._batch_table: str | None = None

    def get_bind(self):
        return self._conn

    def execute(self, *a, **k):  # not used by 0123 but defensive
        return self._conn.execute(*a, **k)

    # ---- table-level ops --------------------------------------------------
    def create_table(self, name, *columns, **kw):
        import sqlalchemy as sa

        col_defs = []
        for col in columns:
            # Resolve the basic SQLite-compatible DDL fragment for the column.
            # Type coercions: UUID→TEXT, Text→TEXT, Boolean→BOOLEAN,
            # DateTime→TIMESTAMP. server_default carried over verbatim except
            # gen_random_uuid() → no-op (SQLite has no built-in UUID gen,
            # and we don't insert without explicit id elsewhere in the test).
            ddl_type = "TEXT"
            t = col.type
            if isinstance(t, sa.Boolean):
                ddl_type = "BOOLEAN"
            elif isinstance(t, sa.DateTime):
                ddl_type = "TIMESTAMP"
            elif isinstance(t, sa.Text):
                ddl_type = "TEXT"
            # else: keep TEXT (covers UUID + everything else used here)

            parts = [f'"{col.name}"', ddl_type]
            if col.primary_key:
                parts.append("PRIMARY KEY")
            if not col.nullable:
                parts.append("NOT NULL")
            if col.server_default is not None:
                txt = str(col.server_default.arg) if hasattr(col.server_default, "arg") else str(col.server_default)
                if "gen_random_uuid" in txt:
                    # Postgres-only function; emulate with a SQLite-friendly
                    # random-hex expression so subsequent INSERTs without an
                    # explicit id PK populate it deterministically. Result
                    # is a 32-char hex string; the migration's INSERT relies
                    # on this default firing.
                    parts.append("DEFAULT (lower(hex(randomblob(16))))")
                elif txt.upper() == "FALSE":
                    parts.append("DEFAULT 0")
                elif txt.upper() == "TRUE":
                    parts.append("DEFAULT 1")
                elif txt.upper().startswith("NOW"):
                    parts.append("DEFAULT CURRENT_TIMESTAMP")
                else:
                    # Literal string default (e.g. 'legacy', '', 'unknown')
                    parts.append(f"DEFAULT {txt}")
            col_defs.append(" ".join(parts))
        sql = f'CREATE TABLE "{name}" ({", ".join(col_defs)})'
        self._conn.execute(sa.text(sql))

    def drop_table(self, name, **kw):
        import sqlalchemy as sa
        self._conn.execute(sa.text(f'DROP TABLE "{name}"'))

    def add_column(self, table, column, **kw):
        import sqlalchemy as sa
        # SQLite-flavored type mapping.
        t = column.type
        ddl_type = "TEXT"
        if isinstance(t, sa.Boolean):
            ddl_type = "BOOLEAN"
        elif isinstance(t, sa.DateTime):
            ddl_type = "TIMESTAMP"
        sql = f'ALTER TABLE "{table}" ADD COLUMN "{column.name}" {ddl_type}'
        self._conn.execute(sa.text(sql))

    def drop_column(self, table, col_name, **kw):
        import sqlalchemy as sa
        self._conn.execute(sa.text(f'ALTER TABLE "{table}" DROP COLUMN "{col_name}"'))

    def alter_column(self, table, col_name, **kw):
        # SQLite doesn't have ALTER COLUMN; server_default changes are
        # silently ignored. The migration's only alter_column call is
        # `server_default=None` (drop the obsolete openclaw default),
        # which is a no-op on SQLite anyway (SQLite stores defaults
        # inline in the table definition; we'd need a table rewrite to
        # change them).
        return None

    # ---- constraint ops (no-op on SQLite) --------------------------------
    def create_check_constraint(self, *a, **kw):
        return None

    def drop_constraint(self, *a, **kw):
        return None

    # ---- batch_alter_table context manager --------------------------------
    def batch_alter_table(self, name, **kw):
        outer = self

        class _BatchCtx:
            def __enter__(self_inner):
                outer._batch_table = name
                return _BatchOp(name, outer)

            def __exit__(self_inner, *exc):
                outer._batch_table = None
                return False

        return _BatchCtx()


class _BatchOp:
    """Inside-batch DDL surface. Translates drop_column/alter_column to the
    parent _SqliteOpShim with the table name baked in."""

    def __init__(self, table, parent):
        self._table = table
        self._parent = parent

    def drop_column(self, col_name, **kw):
        self._parent.drop_column(self._table, col_name, **kw)

    def alter_column(self, col_name, **kw):
        self._parent.alter_column(self._table, col_name, **kw)


def _uuid_to_sqlite_str(v):
    """Translate a uuid.UUID to its SQLite-stored form (32 hex chars, no dashes).

    SQLModel's UUID columns on SQLite store the hex form without separators
    (verified via `SELECT id FROM agents` → '05f011a1fbcb49d8a48fb6632c04379f').
    Production (asyncpg + Postgres native UUID) stores with dashes — the
    distinction matters for `WHERE id = :id` lookups in the SQLite E2E.

    Migration 0122 test analog: line 295 uses `str(henry.id).replace("-", "")`.
    """
    return v.hex if isinstance(v, uuid.UUID) else v


class _SqliteUUIDCoercingProxy:
    """Wraps the sync connection so bind.execute() calls translate
    `uuid.UUID` parameter values to their SQLite-stored str form before
    reaching aiosqlite.

    Production code path (asyncpg) requires native UUID objects per Phase 28
    LIVE-RUN lesson #1. aiosqlite refuses them ("Error binding parameter:
    type 'UUID' is not supported") AND stores them as 32-char hex without
    dashes. This proxy bridges both gaps for the SQLite E2E test without
    altering the migration's production semantics.
    """

    def __init__(self, sync_conn):
        self._conn = sync_conn

    def execute(self, *args, **kwargs):
        # The migration calls bind.execute(sa.text(...), {"id": PyUUID, ...})
        # The second positional arg is the parameter dict (or a list of dicts).
        if len(args) >= 2 and isinstance(args[1], dict):
            coerced = {k: _uuid_to_sqlite_str(v) for k, v in args[1].items()}
            args = (args[0], coerced) + args[2:]
        elif len(args) >= 2 and isinstance(args[1], list):
            coerced_list = []
            for d in args[1]:
                if isinstance(d, dict):
                    coerced_list.append({
                        k: _uuid_to_sqlite_str(v) for k, v in d.items()
                    })
                else:
                    coerced_list.append(d)
            args = (args[0], coerced_list) + args[2:]
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


async def _run_migration_upgrade_against_sqlite(module):
    """Run module.upgrade() against the conftest in-memory SQLite engine.

    Replaces module.op with _SqliteOpShim that translates DDL calls into
    SQLite-native SQL. Wraps the bind connection in _SqliteUUIDCoercingProxy
    so PyUUID(...) bindparams transparently degrade to str for aiosqlite
    (production asyncpg path requires native UUID objects — see Phase 28
    LIVE-RUN lesson #1; the proxy is test-only).

    Cannot use alembic.operations.Operations because the local
    ./alembic/__init__.py (versions dir) shadows the installed package
    when tests run from backend/.
    """
    from tests.conftest import test_engine

    async with test_engine.begin() as conn:

        def _run(sync_conn):
            proxy = _SqliteUUIDCoercingProxy(sync_conn)
            shim = _SqliteOpShim(sync_conn)
            # Override get_bind so the migration sees the coercing proxy.
            shim.get_bind = lambda: proxy
            module.op = shim
            module.upgrade()

        await conn.run_sync(_run)


async def _seed_pre_30_schema(insert_gateways_row: bool = True):
    """Simulate the pre-migration SQLite schema state.

    Plan 30-02 deleted the Gateway SQLModel + `gateway_id` / `gateway_agent_id`
    fields, so SQLModel.metadata.create_all in the conftest autouse fixture
    no longer creates them. This helper hand-builds the missing surface so
    migration 0123 has something to drop.

    Idempotent: DROP TABLE IF EXISTS gateways first (the table is created
    outside SQLModel metadata, so conftest's drop_all between tests doesn't
    clean it up — without IF EXISTS the second test sees a stale table).
    """
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await s.exec(sa_text("DROP TABLE IF EXISTS gateways"))
        await s.exec(sa_text(
            "CREATE TABLE gateways ("
            "  id TEXT PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  url TEXT NOT NULL,"
            "  status TEXT,"
            "  discord_guild_id TEXT,"
            "  discord_category_id TEXT,"
            "  discord_bot_configured BOOLEAN NOT NULL DEFAULT 0,"
            "  created_at TIMESTAMP,"
            "  updated_at TIMESTAMP"
            ")"
        ))
        # ALTER TABLE ... ADD COLUMN is idempotent-unfriendly on SQLite,
        # but agents/boards are dropped+recreated between tests by the
        # autouse setup_db fixture, so each call sees a fresh schema.
        await s.exec(sa_text("ALTER TABLE agents ADD COLUMN gateway_id TEXT"))
        await s.exec(sa_text("ALTER TABLE agents ADD COLUMN gateway_agent_id TEXT"))
        await s.exec(sa_text("ALTER TABLE boards ADD COLUMN gateway_id TEXT"))
        if insert_gateways_row:
            await s.exec(sa_text(
                "INSERT INTO gateways (id, name, url, status, "
                "                       discord_guild_id, discord_category_id, "
                "                       discord_bot_configured) "
                "VALUES (:id, 'legacy', 'http://x', 'unknown', "
                "        '12345', '67890', 1)"
            ).bindparams(id=str(uuid.uuid4())))
        await s.commit()


async def _drop_discord_config_if_present():
    """The migration creates discord_config outside SQLModel metadata IF the
    helper isn't aware of it. Plan 30-02 added DiscordConfig to metadata so
    conftest drop_all DOES drop it between tests. This helper exists as a
    defensive cleanup if a future test runs migration 0123 twice.
    """
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await s.exec(sa_text("DROP TABLE IF EXISTS discord_config"))
        await s.commit()


@pytest.mark.asyncio
async def test_drop_gateway_schema_e2e_sqlite(make_board, make_agent):
    """Full upgrade() against SQLite: seed gateways row + inert agents +
    normal agent, run upgrade(), verify post-state.

    NOTE: SQLite doesn't enforce FK constraints in the conftest fixture
    (PRAGMA off). This E2E checks SQL syntax + business logic, not
    FK-integrity. Post-Plan-30-02, the Gateway SQLModel was deleted, so
    we must manually CREATE TABLE gateways + ALTER TABLE ADD COLUMN to
    simulate the pre-migration state.
    """
    from tests.conftest import test_engine

    await _drop_discord_config_if_present()
    board = await make_board()
    await _seed_pre_30_schema(insert_gateways_row=True)

    # 2 inert openclaw test agents (to be deleted by upgrade())
    await make_agent(name="Bug18Test", board_id=board.id, agent_runtime="openclaw")
    await make_agent(name="D2Test", board_id=board.id, agent_runtime="openclaw")
    # 1 normal agent (must survive)
    await make_agent(name="Boss", board_id=board.id, agent_runtime="host")

    module = _load_migration()
    await _run_migration_upgrade_against_sqlite(module)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Inert agents gone.
        inert_count = (await s.exec(sa_text(
            "SELECT count(*) FROM agents WHERE name IN ('Bug18Test', 'D2Test')"
        ))).first()
        assert inert_count[0] == 0, "Bug18Test + D2Test must be deleted by step 2"

        # Boss survives.
        boss_row = (await s.exec(sa_text(
            "SELECT name, agent_runtime FROM agents WHERE name = 'Boss'"
        ))).first()
        assert boss_row is not None, "Boss must survive the migration"
        assert boss_row[1] == "host"

        # discord_config seeded.
        cfg = (await s.exec(sa_text(
            "SELECT guild_id, category_id, bot_configured FROM discord_config"
        ))).first()
        assert cfg is not None, "discord_config must be seeded from gateways"
        assert cfg[0] == "12345"
        assert cfg[1] == "67890"
        assert bool(cfg[2]) is True

        # Column-drop invariants.
        cols = (await s.exec(sa_text("PRAGMA table_info(agents)"))).all()
        col_names = {c[1] for c in cols}
        assert "workspace_path" in col_names, (
            "workspace_path MUST be preserved (Plan 30-01/02 scope clarification)"
        )
        assert "gateway_id" not in col_names, "agents.gateway_id must be dropped"
        assert "gateway_agent_id" not in col_names, (
            "agents.gateway_agent_id must be dropped"
        )

        board_cols = (await s.exec(sa_text("PRAGMA table_info(boards)"))).all()
        board_col_names = {c[1] for c in board_cols}
        assert "gateway_id" not in board_col_names, "boards.gateway_id must be dropped"

        # gateways table gone (SQLite raises OperationalError on missing
        # table — but PRAGMA table_info(gateways) returns empty list instead
        # of raising. Check via sqlite_master.
        gateways_master = (await s.exec(sa_text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='gateways'"
        ))).first()
        assert gateways_master is None, "gateways table must be dropped"


@pytest.mark.asyncio
async def test_pre_flight_aborts_on_leftover_openclaw_agent(make_board, make_agent):
    """Pre-Flight: if an openclaw agent other than Bug18Test/D2Test exists,
    upgrade() raises RuntimeError BEFORE any mutation (D-09).
    """
    await _drop_discord_config_if_present()
    board = await make_board()
    await _seed_pre_30_schema(insert_gateways_row=False)

    # Stragglers — an openclaw agent not in INERT_TEST_NAMES.
    await make_agent(name="Stragglers", board_id=board.id, agent_runtime="openclaw")

    module = _load_migration()
    with pytest.raises(RuntimeError, match="openclaw"):
        await _run_migration_upgrade_against_sqlite(module)
