"""Add status, confidence, and tracking columns to board_memory.

Lays the schema foundation for the Memory Next-Level upgrade:
- status: draft/published/stale/archived promotion gates
- confidence: high/medium/low trust scoring
- updated_at_content: tracks meaningful content edits (vs. DB row touch)
- last_viewed_at: decay tracking (Redis 30d TTL insufficient for 90d decay)
- contradiction_ids: UUIDs of notes that contradict this one

Revision ID: 0126
Revises: 0125
Create Date: 2026-05-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "board_memory",
        sa.Column("status", sa.String(16), server_default="published", nullable=False),
    )
    op.add_column(
        "board_memory",
        sa.Column("confidence", sa.String(8), server_default="medium", nullable=False),
    )
    op.add_column(
        "board_memory",
        sa.Column("updated_at_content", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "board_memory",
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "board_memory",
        sa.Column("contradiction_ids", sa.JSON(), server_default="[]", nullable=False),
    )
    op.create_index("ix_board_memory_status", "board_memory", ["status"])
    op.create_index("ix_board_memory_confidence", "board_memory", ["confidence"])
    op.create_index("ix_board_memory_last_viewed_at", "board_memory", ["last_viewed_at"])

    # Backfill: set updated_at_content = created_at for existing rows
    op.execute(
        "UPDATE board_memory SET updated_at_content = created_at "
        "WHERE updated_at_content IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_board_memory_last_viewed_at", table_name="board_memory")
    op.drop_index("ix_board_memory_confidence", table_name="board_memory")
    op.drop_index("ix_board_memory_status", table_name="board_memory")
    op.drop_column("board_memory", "contradiction_ids")
    op.drop_column("board_memory", "last_viewed_at")
    op.drop_column("board_memory", "updated_at_content")
    op.drop_column("board_memory", "confidence")
    op.drop_column("board_memory", "status")
