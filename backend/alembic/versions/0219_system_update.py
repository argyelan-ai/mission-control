"""Self-update: system_state (drain + deferred work), system_updates (history),
function update_running_work() (ADR-094 decisions 3 and 7, update plan PR 4c).

* ``system_state(key text primary key, value jsonb, updated_at timestamptz)``
  — the contract the update engine (``scripts/mc_update.py``) writes through
  ``psql``: the row ``key = 'update_drain'`` is the drain. Same table holds
  ``deferred:<kind>:<id>`` rows for work the drain held back.
* ``system_updates`` — one history row per engine run (who, from → to, phase,
  result, backup name).
* ``update_running_work()`` → rows ``(kind, n)`` — the one definition of
  "running work" the engine waits for before it switches code, and that the
  UI shows: tasks ``in_progress`` plus open group-chat rounds of running
  groups. A loop's round is a task and is counted there; due schedules are a
  preflight check, not something to wait for (the drain defers them). A
  function, not a view: Postgres does not track the columns inside a
  string-bodied SQL function, so a later migration that changes the type of
  ``agent_groups.status`` or ``group_rounds.finished_at`` is not blocked by
  it. A migration that renames one of the columns it reads must replace the
  function in the same step.

Additive only. Downgrade drops the function and both tables; the journal on
the host keeps the truth about runs.

Revision ID: 0219_system_update
Revises: 0218_config_checks
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0219_system_update"
down_revision = "0218_config_checks"
branch_labels = None
depends_on = None

RUNNING_WORK_FUNCTION = """
create or replace function update_running_work()
returns table (kind text, n integer)
language sql stable
as $$
select 'tasks'::text, count(*)::int
  from tasks
 where status = 'in_progress'
union all
select 'group_rounds'::text, count(*)::int
  from group_rounds r
  join agent_groups g on g.id = r.group_id
 where r.finished_at is null
   and g.status = 'running'
$$
"""


def upgrade() -> None:
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "system_updates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="ui"),
        sa.Column("from_version", sa.String(length=40), nullable=True),
        sa.Column("target_version", sa.String(length=40), nullable=True),
        sa.Column("phase", sa.String(length=24), nullable=True),
        sa.Column("phase_state", sa.String(length=24), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("failure_kind", sa.String(length=32), nullable=True),
        sa.Column("backup", sa.String(length=200), nullable=True),
        sa.Column("started_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("started_by_name", sa.String(length=200), nullable=True),
        sa.Column("rollback_requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("rollback_requested_by_name", sa.String(length=200), nullable=True),
        sa.Column("rollback_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drain_lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_system_updates_run_id", "system_updates", ["run_id"], unique=True)
    op.execute(RUNNING_WORK_FUNCTION)


def downgrade() -> None:
    op.execute("drop function if exists update_running_work()")
    op.drop_index("ix_system_updates_run_id", table_name="system_updates")
    op.drop_table("system_updates")
    op.drop_table("system_state")
