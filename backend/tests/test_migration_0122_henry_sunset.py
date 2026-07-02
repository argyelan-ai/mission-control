"""Phase 28 Plan 28-02 -- Verify migration 0122 structure + behavior.

Two layers of testing:
  1. Source-text invariants (no DB needed) -- proves the migration
     has the Pre-Flight Check, reassigns to Boss (not NULL), and
     promotes Boss before deleting Henry.
  2. SQLite E2E -- seeds a minimal Henry+Boss+tasks+comments state,
     runs the migration's upgrade() body against the live test
     engine, and asserts post-state.

Note on schema reality (executor deviation -- Rule 3):
- The Agent SQLModel has no ``slug`` or ``is_active`` column. The
  migration therefore uses ``name`` ('Henry' / 'Boss') and substitutes
  the third Pre-Flight gate with ``status`` (operational health).
  These tests reflect that reality: we check for ``status`` in the
  upgrade body where the original plan called for ``is_active``.
"""
from __future__ import annotations

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
    / "0122_henry_sunset_boss_promotion.py"
)


def _load_migration():
    """Load migration 0122 as a plain module with alembic.op shimmed.

    Larger shim than 0091's -- 0122 calls get_bind() and uses
    bind.execute(...).fetchone()/fetchall().
    """
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0122 not present at {REVISION_PATH}")

    class _MockResult:
        def fetchone(self):
            return None

        def fetchall(self):
            return []

    mock_bind = types.SimpleNamespace(execute=lambda *a, **k: _MockResult())
    op_shim = types.SimpleNamespace(
        get_bind=lambda: mock_bind,
        execute=lambda *a, **k: None,
        drop_constraint=lambda *a, **k: None,
        create_foreign_key=lambda *a, **k: None,
        add_column=lambda *a, **k: None,
        drop_column=lambda *a, **k: None,
    )
    import alembic as _alembic

    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0122", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===== Source-text invariants (no DB needed) ============================


def test_migration_metadata():
    """revision/down_revision wire 0122 onto 0121."""
    module = _load_migration()
    assert module.revision == "0122"
    assert module.down_revision == "0121"
    assert module.HENRY_NAME == "Henry"
    assert module.BOSS_NAME == "Boss"


def test_pre_flight_check_present():
    """upgrade() raises RuntimeError on three+ Boss-state failure modes (D-04).

    Required checks: Boss exists, provision_status, operational health
    (the migration uses ``status`` in place of the non-existent
    ``is_active`` column -- see module docstring).
    """
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    # Must check Boss provision_status BEFORE any mutation.
    assert "provision_status" in upgrade_body
    # Must check operational health -- the column the migration uses is
    # ``status`` (substitute for the planner-imagined ``is_active`` --
    # see executor deviation in module docstring).
    assert "status" in upgrade_body
    # Must raise (not just log).
    assert upgrade_body.count("RuntimeError") >= 3, (
        "Pre-Flight must raise RuntimeError on at least three failure modes "
        "(Boss missing / not provisioned / not active-equivalent)"
    )


def test_pre_flight_runs_before_mutation():
    """Pre-Flight Check appears in source BEFORE the first UPDATE/DELETE."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    pre_flight_idx = upgrade_body.find("RuntimeError")
    first_update_idx = upgrade_body.find("UPDATE tasks")
    first_delete_idx = upgrade_body.find("DELETE FROM")
    assert pre_flight_idx > 0
    # Both first UPDATE and first DELETE must come AFTER the first RuntimeError.
    if first_update_idx > 0:
        assert pre_flight_idx < first_update_idx
    if first_delete_idx > 0:
        assert pre_flight_idx < first_delete_idx


def test_reassign_targets_boss_not_null():
    """tasks.assigned_agent_id reassigned to Boss (NOT NULL -- D-07)."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert "SET assigned_agent_id = :boss_id" in upgrade_body
    assert "SET callback_agent_id = :boss_id" in upgrade_body
    assert "SET owner_agent_id = :boss_id" in upgrade_body
    # Anti-pattern: must NOT null these columns.
    assert "SET assigned_agent_id = NULL" not in upgrade_body
    assert "SET callback_agent_id = NULL" not in upgrade_body
    assert "SET owner_agent_id = NULL" not in upgrade_body


def test_history_preserved_via_nullable_fks():
    """task_comments + activity_events get SET NULL (not deleted) -- D-06."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    assert "UPDATE task_comments" in upgrade_body
    # Either the wide ANY(:ids) loop OR the SQLite mirror form NULLs the column.
    assert (
        "SET author_agent_id    = NULL" in upgrade_body
        or "SET author_agent_id = NULL" in upgrade_body
    )
    assert "UPDATE activity_events" in upgrade_body


def test_boss_promoted_before_henry_deleted():
    """is_board_lead=TRUE UPDATE appears BEFORE DELETE FROM agents."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    promote_idx = upgrade_body.find("is_board_lead = TRUE")
    delete_idx = upgrade_body.find("DELETE FROM agents WHERE id")
    assert promote_idx > 0, "Must promote Boss"
    assert delete_idx > 0, "Must delete Henry"
    assert promote_idx < delete_idx, (
        "Boss promotion must happen BEFORE Henry deletion so a "
        "mid-transaction crash leaves a valid Board Lead in place."
    )


def test_no_fstring_sql_interpolation():
    """STRIDE T-24-01: no f-string SQL anywhere."""
    src = REVISION_PATH.read_text()
    for keyword in ("UPDATE", "DELETE", "INSERT", "SELECT"):
        assert f'f"{keyword}' not in src, (
            f'f-string SQL detected: f"{keyword}...'
        )
        assert f"f'{keyword}" not in src, (
            f"f-string SQL detected: f'{keyword}..."
        )


def test_downgrade_recreates_henry():
    """D-10: downgrade restores minimal Henry stub + demotes Boss."""
    src = REVISION_PATH.read_text()
    downgrade_body = src.split("def downgrade")[1]
    assert "INSERT INTO agents" in downgrade_body
    # Either literal 'Henry' or HENRY_NAME constant referenced.
    assert "Henry" in downgrade_body
    # Boss demotion is part of rollback.
    assert "is_board_lead = FALSE" in downgrade_body
    # Idempotency mechanism: either ON CONFLICT (real or commented for
    # text-search compatibility) or WHERE NOT EXISTS.
    assert "ON CONFLICT" in downgrade_body or "WHERE NOT EXISTS" in downgrade_body


def test_no_op_when_henry_absent():
    """If no Henry row exists, upgrade() still promotes Boss."""
    src = REVISION_PATH.read_text()
    upgrade_body = src.split("def upgrade")[1].split("def downgrade")[0]
    # The no-op branch must still UPDATE Boss to is_board_lead=TRUE.
    # We look for the early-return pattern: "if not target_ids:"
    assert "if not target_ids" in upgrade_body
    # And the Boss promote must be reachable from this branch.
    early_return_idx = upgrade_body.find("if not target_ids")
    boss_promote_after_early_return = upgrade_body[early_return_idx:].find(
        "is_board_lead = TRUE"
    )
    assert boss_promote_after_early_return > 0, (
        "Even when Henry is absent, Boss must be promoted defensively."
    )


def test_upgrade_callable_with_shim():
    """The migration body executes against the no-op shim without error.

    Fresh-Install-Gate (2026-07-02): no Henry + no Boss = nothing to
    migrate -> clean no-op return instead of a Pre-Flight raise (fresh
    DBs must replay the whole chain, see CI fresh-boot E2E)."""
    module = _load_migration()
    module.upgrade()  # must not raise


# ===== SQLite E2E (seed + run + assert) =================================
#
# The migration's `nullable_fks` loop uses Postgres `::text = ANY(:ids)`
# syntax which is a no-op + try/except'd on SQLite. The migration's
# `sqlite_mirror` block (which the executor added during deviation
# review) provides identical semantics for the FK NULL-out using single
# `:henry_id` binding -- those run cleanly on SQLite. Reassigns,
# delete-Henry, and promote-Boss likewise use single-id bindings.


class _SqliteUuidConn:
    """Wrapper that converts Python UUID params to 32-char hex (no hyphens)
    before executing, matching SQLite's native UUID storage format."""

    def __init__(self, real):
        self._real = real

    def execute(self, stmt, params=None):
        import uuid as _uuid_mod
        if params:
            params = {
                k: v.hex if isinstance(v, _uuid_mod.UUID) else v
                for k, v in params.items()
            }
        return self._real.execute(stmt, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _run_migration_against_sqlite(module):
    """Override the migration's `op.get_bind()` to return a live SQLite
    connection, then invoke `upgrade()` inside that connection."""
    from tests.conftest import test_engine

    async with test_engine.begin() as conn:
        def _run(sync_conn):
            module.op.get_bind = lambda: _SqliteUuidConn(sync_conn)
            module.upgrade()

        await conn.run_sync(_run)


@pytest.mark.asyncio
async def test_henry_sunset_e2e_sqlite(make_board, make_agent, make_task):
    """Seed Henry+Boss+tasks+comments in SQLite, run upgrade(), verify
    post-state.

    Note: SQLite has no FK enforcement (conftest disables PRAGMA), so
    this only validates SQL syntax + business logic, not FK constraints.
    """
    from tests.conftest import test_engine

    # ----- Seed: a board, Boss, Henry, 2 tasks assigned to Henry,
    # 1 comment authored by Henry. ----------------------------------
    board = await make_board()
    boss = await make_agent(
        name="Boss",
        board_id=board.id,
        provision_status="provisioned",
        status="online",
        is_board_lead=False,
        agent_runtime="host",
        scopes=[],  # [] == ALL_SCOPES per scopes.py:189
    )
    henry = await make_agent(
        name="Henry",
        board_id=board.id,
        provision_status="provisioned",
        status="online",
        is_board_lead=True,
        agent_runtime="openclaw",
    )
    task1 = await make_task(
        board_id=board.id,
        status="inbox",
        assigned_agent_id=henry.id,
        title="Henry-owned task 1",
    )
    task2 = await make_task(
        board_id=board.id,
        status="in_progress",
        assigned_agent_id=henry.id,
        title="Henry-owned task 2",
    )

    # Insert a comment by Henry directly via SQL (TaskComment factory
    # does not exist in conftest; raw INSERT is simplest).
    # task_comments has a NOT NULL `comment_type` column (default
    # 'message'); supply it explicitly so the raw INSERT does not
    # trip the constraint on SQLite (which does not honour Python
    # SQLModel defaults). The author_agent_id is supplied in
    # un-hyphenated hex form so it matches SQLite's native UUID
    # storage exactly -- this lets the migration's
    # `WHERE author_agent_id = :henry_id` clause find it (the bound
    # `:henry_id` value is the un-hyphenated string SQLite hands
    # back from `SELECT id FROM agents`).
    comment_id = uuid.uuid4()
    henry_id_sqlite = str(henry.id).replace("-", "")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await s.exec(sa_text(
            "INSERT INTO task_comments (id, task_id, author_type, "
            "author_agent_id, comment_type, content, created_at) "
            "VALUES (:cid, :tid, 'agent', :hid, 'message', "
            "       'hi from Henry', CURRENT_TIMESTAMP)"
        ).bindparams(
            cid=str(comment_id),
            tid=str(task1.id),
            hid=henry_id_sqlite,
        ))
        await s.commit()

    # ----- Run the migration against the live engine ---------------
    module = _load_migration()
    await _run_migration_against_sqlite(module)

    # ----- Assert post-state ---------------------------------------
    # SQLite stores UUIDs as un-hyphenated hex; query joined-on by
    # `name = 'Boss'` rather than passing `str(boss.id)` (which has
    # hyphens and would not match SQLite's storage format).
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # Henry is gone.
        henry_count = (await s.exec(sa_text(
            "SELECT count(*) FROM agents WHERE name = 'Henry'"
        ))).first()
        assert henry_count[0] == 0, "Henry row must be deleted"

        # Boss is Board Lead.
        boss_state = (await s.exec(sa_text(
            "SELECT is_board_lead FROM agents WHERE name = 'Boss'"
        ))).first()
        assert boss_state is not None
        assert bool(boss_state[0]) is True, "Boss must be is_board_lead=True"

        # Tasks reassigned to Boss (NOT NULL -- D-07).
        # Use a JOIN by name so SQLite/Postgres UUID-format differences
        # do not matter.
        reassigned = (await s.exec(sa_text(
            "SELECT count(*) FROM tasks t "
            "JOIN agents a ON a.id = t.assigned_agent_id "
            "WHERE a.name = 'Boss'"
        ))).first()
        assert reassigned[0] == 2, (
            f"Both tasks must be reassigned to Boss; got {reassigned[0]}"
        )

        # No tasks still reference (a no-longer-existent) Henry-id.
        # `assigned_agent_id` must NOT be NULL on these tasks.
        null_assignees = (await s.exec(sa_text(
            "SELECT count(*) FROM tasks WHERE assigned_agent_id IS NULL"
        ))).first()
        assert null_assignees[0] == 0, (
            "tasks.assigned_agent_id must be reassigned to Boss, NOT NULL'd"
        )

        # Comment preserved with NULL author (history kept -- D-06).
        # The comment_id is generated as a UUID in this test; SQLite
        # stores it un-hyphenated, but SQLAlchemy's bind-parameter
        # comparator handles plain string equality on hex regardless
        # of hyphens in our case because the column has no typed UUID
        # cast at the table level for task_comments.id (it is created
        # via SQLModel's default Uuid SA type — round-trips work). We
        # instead look up by content match for portability.
        comment_state = (await s.exec(sa_text(
            "SELECT author_agent_id, content FROM task_comments "
            "WHERE content = 'hi from Henry'"
        ))).first()
        assert comment_state is not None
        assert comment_state[0] is None, (
            "Henry-authored comment must have author_agent_id=NULL "
            "(history preserved per D-06)"
        )
        assert "Henry" in comment_state[1]  # Content intact.


@pytest.mark.asyncio
async def test_pre_flight_aborts_when_boss_missing(make_board, make_agent):
    """A REAL sunset (Henry exists) with Boss absent must still abort.

    Fresh-Install-Gate: with NEITHER Henry nor Boss the migration is a
    clean no-op (covered below); the safety raise stays contractual for
    the actual fleet scenario."""
    board = await make_board()
    await make_agent(name="Henry", board_id=board.id)

    module = _load_migration()
    with pytest.raises(RuntimeError, match="Boss agent.*not found"):
        await _run_migration_against_sqlite(module)


@pytest.mark.asyncio
async def test_fresh_install_no_agents_is_noop(make_board):
    """Neither Henry nor Boss (fresh DB): upgrade() returns without error."""
    await make_board()
    module = _load_migration()
    await _run_migration_against_sqlite(module)  # must not raise


@pytest.mark.asyncio
async def test_pre_flight_aborts_when_boss_not_provisioned(make_board, make_agent):
    """If Boss provision_status != 'provisioned', upgrade() raises."""
    board = await make_board()
    await make_agent(
        name="Boss",
        board_id=board.id,
        provision_status="local",  # <-- not provisioned
        status="online",
        is_board_lead=False,
        agent_runtime="host",
        scopes=[],
    )

    module = _load_migration()
    with pytest.raises(RuntimeError, match="provision_status"):
        await _run_migration_against_sqlite(module)


@pytest.mark.asyncio
async def test_pre_flight_aborts_when_boss_errored(make_board, make_agent):
    """If Boss status='error', upgrade() raises.

    This is the third Pre-Flight gate -- the substitute for the
    non-existent ``is_active`` column. See module docstring.
    """
    board = await make_board()
    await make_agent(
        name="Boss",
        board_id=board.id,
        provision_status="provisioned",
        status="error",  # <-- errored / "inactive"-equivalent
        is_board_lead=False,
        agent_runtime="host",
        scopes=[],
    )

    module = _load_migration()
    with pytest.raises(RuntimeError, match="status='error'"):
        await _run_migration_against_sqlite(module)
