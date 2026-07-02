"""Add dispatch_attempt_id for stale update protection.

Revision ID: 0035
Revises: 0034
"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("dispatch_attempt_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "dispatch_attempt_id")
