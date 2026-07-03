"""schedule_enhancements

Revision ID: 0021
Revises: 0020
Create Date: 2026-03-01

New table: scheduled_job_runs + new columns on scheduled_jobs.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New table: scheduled_job_runs ──────────────────────────────────
    op.create_table(
        "scheduled_job_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("retry_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["scheduled_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduled_job_runs_job_id", "scheduled_job_runs", ["job_id"])
    op.create_index(
        "ix_scheduled_job_runs_started_at", "scheduled_job_runs", ["started_at"]
    )

    # ── New columns on scheduled_jobs ──────────────────────────────────
    # Retry
    op.add_column(
        "scheduled_jobs",
        sa.Column("retry_max", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "scheduled_jobs",
        sa.Column(
            "retry_delay_minutes", sa.Integer(), nullable=False, server_default="5"
        ),
    )
    # Dependencies (self-referential FK — manual, not via autogenerate)
    op.add_column(
        "scheduled_jobs",
        sa.Column(
            "depends_on_job_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_scheduled_jobs_depends_on",
        "scheduled_jobs",
        "scheduled_jobs",
        ["depends_on_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Notifications
    op.add_column(
        "scheduled_jobs",
        sa.Column(
            "notify_on_failure", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    # create_task action
    op.add_column(
        "scheduled_jobs",
        sa.Column("task_board_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scheduled_jobs",
        sa.Column("task_title", sa.Text(), nullable=True),
    )
    op.add_column(
        "scheduled_jobs",
        sa.Column("task_priority", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_scheduled_jobs_task_board",
        "scheduled_jobs",
        "boards",
        ["task_board_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_scheduled_jobs_task_board", "scheduled_jobs", type_="foreignkey"
    )
    op.drop_column("scheduled_jobs", "task_priority")
    op.drop_column("scheduled_jobs", "task_title")
    op.drop_column("scheduled_jobs", "task_board_id")
    op.drop_column("scheduled_jobs", "notify_on_failure")
    op.drop_constraint(
        "fk_scheduled_jobs_depends_on", "scheduled_jobs", type_="foreignkey"
    )
    op.drop_column("scheduled_jobs", "depends_on_job_id")
    op.drop_column("scheduled_jobs", "retry_delay_minutes")
    op.drop_column("scheduled_jobs", "retry_max")
    op.drop_index("ix_scheduled_job_runs_started_at", table_name="scheduled_job_runs")
    op.drop_index("ix_scheduled_job_runs_job_id", table_name="scheduled_job_runs")
    op.drop_table("scheduled_job_runs")
