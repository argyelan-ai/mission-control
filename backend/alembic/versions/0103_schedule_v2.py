"""schedule_v2

Revision ID: 0103
Revises: 0102
Create Date: 2026-05-04 21:00:00

Extends scheduler tables for Schedule v2:
- scheduled_jobs: cron expression, weekday list, date range, task_payload,
  tags, snooze, consecutive_failures
- scheduled_job_runs: task_id FK
- Data migration: backfill task_payload from legacy columns, disable
  unsupported action types
- New composite index on (enabled, next_run_at)
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0103'
down_revision: Union[str, None] = '0102'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── scheduled_jobs: new columns ──────────────────────────────────────────

    # Cron expression (e.g. "0 9 * * 1,3,5")
    op.add_column(
        'scheduled_jobs',
        sa.Column('schedule_cron', sa.String(), nullable=True),
    )

    # Weekly-custom weekday list [0-6]
    op.add_column(
        'scheduled_jobs',
        sa.Column('schedule_weekdays', postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )

    # Active date window
    op.add_column(
        'scheduled_jobs',
        sa.Column('start_date', sa.Date(), nullable=True),
    )
    op.add_column(
        'scheduled_jobs',
        sa.Column('end_date', sa.Date(), nullable=True),
    )

    # Full task creation payload (replaces individual task_* fields over time)
    op.add_column(
        'scheduled_jobs',
        sa.Column(
            'task_payload',
            postgresql.JSON(astext_type=sa.Text()),
            nullable=True,
            server_default='{}',
        ),
    )

    # String tags for categorisation
    op.add_column(
        'scheduled_jobs',
        sa.Column(
            'tags',
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default='[]',
        ),
    )

    # Snooze: skip all firings until this timestamp
    op.add_column(
        'scheduled_jobs',
        sa.Column('snoozed_until', sa.DateTime(timezone=True), nullable=True),
    )

    # Consecutive failure counter (reset to 0 on success)
    op.add_column(
        'scheduled_jobs',
        sa.Column(
            'consecutive_failures',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )

    # ── scheduled_job_runs: task_id FK ───────────────────────────────────────
    op.add_column(
        'scheduled_job_runs',
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_scheduled_job_runs_task_id',
        'scheduled_job_runs',
        'tasks',
        ['task_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # ── Data migration ────────────────────────────────────────────────────────

    # Backfill task_payload from legacy columns for create_task jobs
    op.execute(
        """
        UPDATE scheduled_jobs
        SET task_payload = jsonb_build_object(
            'board_id',    task_board_id::text,
            'title',       COALESCE(task_title, ''),
            'priority',    COALESCE(task_priority, 'medium'),
            'skip_review', COALESCE(task_skip_review, false)
        )
        WHERE action_type = 'create_task'
        """
    )

    # Disable legacy action types that are no longer supported
    op.execute(
        """
        UPDATE scheduled_jobs
        SET enabled = false
        WHERE action_type IN ('chat_send', 'api_call', 'session_reset', 'run_meeting')
        """
    )

    # Backfill scheduled_job_runs.task_id from detail JSON
    op.execute(
        """
        UPDATE scheduled_job_runs r
        SET task_id = (r.detail->>'task_id')::uuid
        WHERE r.detail->>'task_id' IS NOT NULL
        """
    )

    # ── Index ─────────────────────────────────────────────────────────────────
    op.create_index(
        'ix_scheduled_jobs_enabled_next_run_at',
        'scheduled_jobs',
        ['enabled', 'next_run_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_scheduled_jobs_enabled_next_run_at', table_name='scheduled_jobs')

    op.drop_constraint(
        'fk_scheduled_job_runs_task_id', 'scheduled_job_runs', type_='foreignkey'
    )
    op.drop_column('scheduled_job_runs', 'task_id')

    op.drop_column('scheduled_jobs', 'consecutive_failures')
    op.drop_column('scheduled_jobs', 'snoozed_until')
    op.drop_column('scheduled_jobs', 'tags')
    op.drop_column('scheduled_jobs', 'task_payload')
    op.drop_column('scheduled_jobs', 'end_date')
    op.drop_column('scheduled_jobs', 'start_date')
    op.drop_column('scheduled_jobs', 'schedule_weekdays')
    op.drop_column('scheduled_jobs', 'schedule_cron')
