"""Add spawn tracking fields to tasks.

Revision ID: 0033
Revises: 0032
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("spawn_run_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("spawn_session_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "spawn_session_key")
    op.drop_column("tasks", "spawn_run_id")
