"""Add knowledge base fields to board_memory

Revision ID: 0005
Revises: 0004
Create Date: 2026-02-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns
    op.add_column("board_memory", sa.Column("title", sa.Text(), nullable=True))
    op.add_column(
        "board_memory",
        sa.Column("agent_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "board_memory",
        sa.Column("linked_ids", JSONB, server_default="[]", nullable=False),
    )
    op.add_column(
        "board_memory",
        sa.Column("auto_generated", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "board_memory",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )

    # Make board_id nullable (enables global/agent-scoped entries)
    op.alter_column("board_memory", "board_id", existing_type=sa.Uuid(), nullable=True)

    # Add foreign key for agent_id
    op.create_foreign_key(
        "fk_board_memory_agent_id",
        "board_memory",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Add indexes for new query patterns
    op.create_index("idx_board_memory_agent", "board_memory", ["agent_id"])
    op.create_index("idx_board_memory_type", "board_memory", ["memory_type"])
    op.create_index("idx_board_memory_auto", "board_memory", ["auto_generated"])


def downgrade() -> None:
    op.drop_index("idx_board_memory_auto")
    op.drop_index("idx_board_memory_type")
    op.drop_index("idx_board_memory_agent")
    op.drop_constraint("fk_board_memory_agent_id", "board_memory", type_="foreignkey")
    op.drop_column("board_memory", "updated_at")
    op.drop_column("board_memory", "auto_generated")
    op.drop_column("board_memory", "linked_ids")
    op.drop_column("board_memory", "agent_id")
    op.drop_column("board_memory", "title")
    op.alter_column("board_memory", "board_id", existing_type=sa.Uuid(), nullable=False)
