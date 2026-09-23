"""Real-DB proof that 0201 is additive-only (Task 993840da / cab1bfdd).

0201 used to add operator_language/work_language AND drop `language` in
one transaction. On a scratch DB that is correct, but production keeps
serving reads/writes against `agents` while the migration runs, and the
not-yet-restarted old code still reads `language` — dropping it there
breaks every query against `agents` between the DROP and the backend
bounce. So 0201 is additive only.

The contract step that drops `language` (originally 0202) is deliberately
NOT in this branch/PR — see `deferred/0202-drop-agent-language` for its
unchanged content. Task cab1bfdd: with 0202 on this branch it was the
alembic head, and `docker-entrypoint.sh` runs `alembic upgrade head` on
every deploy — the first deploy after merging would have run 0201 and
0202 back to back and dropped the column anyway, defeating the whole
point of the split. So the drop migration itself has to be a later,
separate PR, not just a separate revision file sitting on this branch.

This test does NOT use the shim-`op` pattern the older
``test_migration_0*.py`` files use (a no-op stand-in that never touches a
real connection, plus separately checking that ``test_engine`` — built
from *current* models — already has the columns). That pattern can't
prove this card's point at all, because ``test_engine``'s schema is the
*already-split* shape; it never had `language` to begin with. Instead:

- builds a scratch SQLite `agents` table shaped exactly like TODAY's
  production table (has `language`, lacks operator_language/work_language)
- runs the REAL 0201 upgrade() (real `op`, bound to a real connection)
  against it
- proves `language` survives, is still populated, and the new columns
  are backfilled/defaulted correctly
- loads the row back through this branch's real `Agent` ORM class and
  runs the exact expressions `dispatch_message_builder.py` /
  `template_renderer.py` use, off the real loaded row — proving the
  current backend code tolerates the old column being present
- exercises the 0201 downgrade path
"""
from __future__ import annotations

import ast
import datetime
import importlib.util
import pathlib
import sys
import types
import uuid

import pytest
from sqlalchemy import MetaData, Table, inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel, select

from app.models.agent import Agent

VERSIONS_DIR = pathlib.Path(__file__).parents[1] / "alembic" / "versions"


def _load_revision(name: str):
    """Load a migration file as a plain module. Same shim-injection pattern
    as test_migration_0162.py: the file's ``from alembic import op`` needs
    *something* bound at exec time — we hand it a placeholder here and swap
    in the real, connection-bound ``_RealOp`` right before calling
    upgrade()/downgrade().
    """
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


class _RealOp:
    """Binds alembic's op.add_column/drop_column/execute to a real sync connection."""

    def __init__(self, conn):
        self.conn = conn

    def add_column(self, table, column):
        from sqlalchemy.schema import CreateColumn

        col_ddl = str(CreateColumn(column).compile(dialect=self.conn.dialect))
        self.conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_ddl}"))

    def drop_column(self, table, column_name):
        self.conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column_name}"))

    def execute(self, sql):
        self.conn.execute(text(sql))


async def _get_columns(engine, table_name):
    async with engine.connect() as conn:
        return {
            c["name"]
            for c in await conn.run_sync(lambda sc: sa_inspect(sc).get_columns(table_name))
        }


async def test_0201_stays_additive(tmp_path):
    db_path = tmp_path / "expand_proof.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    # ── scratch table shaped like TODAY's production `agents` table ──
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: SQLModel.metadata.create_all(sc, tables=[Agent.__table__]))
        await conn.execute(text("ALTER TABLE agents ADD COLUMN language VARCHAR(16) NOT NULL DEFAULT 'en'"))
        await conn.execute(text("ALTER TABLE agents DROP COLUMN operator_language"))
        await conn.execute(text("ALTER TABLE agents DROP COLUMN work_language"))

    cols = await _get_columns(engine, "agents")
    assert "language" in cols
    assert "operator_language" not in cols
    assert "work_language" not in cols

    # ── one real fleet-shaped row, language='de' (today's production shape) ──
    transient = Agent(name="Rex", agent_runtime="cli-bridge")
    values = {
        c.name: getattr(transient, c.name)
        for c in Agent.__table__.columns
        if c.name not in ("operator_language", "work_language")
    }
    values["id"] = str(uuid.uuid4())
    values["created_at"] = values["updated_at"] = datetime.datetime.utcnow()
    values["language"] = "de"

    meta = MetaData()
    async with engine.begin() as conn:
        old_agents_table = await conn.run_sync(
            lambda sc: Table("agents", meta, autoload_with=sc, resolve_fks=False)
        )
        await conn.execute(old_agents_table.insert().values(**values))

    # ── REAL 0201 upgrade() against a REAL connection ──
    mig0201 = _load_revision("0201_agent_op_work_language")

    def _run_0201_upgrade(sc):
        mig0201.op = _RealOp(sc)
        mig0201.upgrade()

    async with engine.begin() as conn:
        await conn.run_sync(_run_0201_upgrade)

    cols = await _get_columns(engine, "agents")
    assert {"language", "operator_language", "work_language"} <= cols, cols

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT language, operator_language, work_language FROM agents WHERE name='Rex'")
            )
        ).fetchone()
    assert row.language == "de"
    assert row.operator_language == "de"  # backfilled from the still-present `language`
    assert row.work_language == "en"  # server_default

    # ── the point of the card: current backend code tolerates `language`
    #    still being present, and reads the new columns correctly ──
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.execute(select(Agent).where(Agent.name == "Rex"))
        agent = result.scalar_one()
        assert not hasattr(type(agent), "language")  # model doesn't map it — extra column is harmless

        # exact expressions dispatch_message_builder.py / template_renderer.py use
        operator_lang = (getattr(agent, "operator_language", "en") or "en").lower()
        work_lang = (getattr(agent, "work_language", "en") or "en").lower()
        assert operator_lang == "de"
        assert work_lang == "en"

    # ── 0201 downgrade path ──
    def _run_0201_downgrade(sc):
        mig0201.op = _RealOp(sc)
        mig0201.downgrade()

    async with engine.begin() as conn:
        await conn.run_sync(_run_0201_downgrade)

    cols = await _get_columns(engine, "agents")
    assert "operator_language" not in cols
    assert "work_language" not in cols
    assert "language" in cols

    await engine.dispose()


def test_alembic_chain_0200_0201_single_head():
    """0201 revises 0200. The drop of `language` (planned as
    0202_drop_agent_language) is deliberately not on this branch (task cab1bfdd); it
    lives unchanged on `deferred/0202-drop-agent-language` until its own,
    later PR.

    Head-uniqueness across the *whole* chain is already covered by
    ``test_alembic_chain_integrity.py::test_exactly_one_head`` (ast-based,
    handles merge revisions with tuple ``down_revision``). This test only
    adds the specific claim that the contract step (dropping `language`)
    is not part of the chain. The head id itself is not pinned, so later
    migrations do not have to edit this test.
    """
    mig0201 = _load_revision("0201_agent_op_work_language")
    assert mig0201.revision == "0201_agent_op_work_language"
    assert mig0201.down_revision == "0200_task_pr_reference"

    assert not (VERSIONS_DIR / "0202_drop_agent_language.py").exists(), (
        "0202 is back on this branch — task cab1bfdd took it off deliberately "
        "(see deferred/0202-drop-agent-language)"
    )

    revs: set[str] = set()
    parents: set[str] = set()
    for f in VERSIONS_DIR.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            elif isinstance(node, ast.Assign):
                targets = node.targets
            else:
                continue
            for target in targets:
                name = getattr(target, "id", None)
                if name == "revision":
                    revs.add(ast.literal_eval(node.value))
                elif name == "down_revision":
                    val = ast.literal_eval(node.value)
                    if val is None:
                        continue
                    parents.update([val] if isinstance(val, str) else val)
    heads = revs - parents
    # Do not pin the head to one revision id: every later migration would
    # have to edit this test. The claim here is only that the contract step
    # (dropping `language`) is not part of the chain and there is one head.
    assert len(heads) == 1, heads
    assert "0202_drop_agent_language" not in revs, revs
    assert "0201_agent_op_work_language" in parents, parents


def test_0201_upgrade_does_not_drop_language():
    """Source-level guard: 0201's upgrade() body must not contain a DROP of
    `language` — that's exactly the regression this card exists to prevent.
    """
    source = (VERSIONS_DIR / "0201_agent_op_work_language.py").read_text()
    assert 'drop_column("agents", "language")' not in source
