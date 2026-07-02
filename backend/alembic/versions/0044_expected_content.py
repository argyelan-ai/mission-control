"""Add expected_content to tasks for visual_proof validation.

Revision ID: 0044
Revises: 0043
"""
from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("expected_content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "expected_content")
