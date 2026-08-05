"""agents.archived_at — timestamptz like every other datetime on Agent.

The column was created as a naive TIMESTAMP while agent_lifecycle stamps it
with the tz-aware app.utils.utcnow() — asyncpg refuses to encode an aware
datetime into a naive column, so POST /agents/{id}/archive 500'd on every
call (live finding 2026-08-05, demo-seed cleanup). Existing values were
written as UTC, so the USING clause reinterprets them losslessly.

Revision ID: 0175_agent_archived_at_tz
Revises: 0174_task_origin_thread
"""
import sqlalchemy as sa

from alembic import op

revision = "0175_agent_archived_at_tz"
down_revision = "0174_task_origin_thread"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agents",
        "archived_at",
        type_=sa.DateTime(timezone=True),
        postgresql_using="archived_at AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    op.alter_column(
        "agents",
        "archived_at",
        type_=sa.DateTime(timezone=False),
        postgresql_using="archived_at AT TIME ZONE 'UTC'",
    )
