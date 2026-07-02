"""board_memory archived_at

Revision ID: 0113
Revises: 0112
Create Date: 2026-05-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "board_memory",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "board_memory",
        sa.Column("archive_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "board_memory",
        sa.Column("archive_bucket", sa.String(length=8), nullable=True),
    )
    op.create_index(
        "ix_board_memory_archived_at",
        "board_memory",
        ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_board_memory_archived_at", table_name="board_memory")
    op.drop_column("board_memory", "archive_bucket")
    op.drop_column("board_memory", "archive_reason")
    op.drop_column("board_memory", "archived_at")
