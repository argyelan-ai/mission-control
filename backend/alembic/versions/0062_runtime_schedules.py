"""runtime_schedules + runtime_schedule_runs tables

Revision ID: 0062
Revises: 0061
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("runtime_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("time_of_day", sa.String(), nullable=False),
        sa.Column("days", sa.String(), nullable=False),
        sa.Column("unload_first", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "runtime_schedule_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "schedule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_schedules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
    )
    op.create_index("ix_runtime_schedule_runs_schedule_id", "runtime_schedule_runs", ["schedule_id"])


def downgrade() -> None:
    op.drop_index("ix_runtime_schedule_runs_schedule_id", table_name="runtime_schedule_runs")
    op.drop_table("runtime_schedule_runs")
    op.drop_table("runtime_schedules")
