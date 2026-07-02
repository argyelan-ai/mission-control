"""Task Checkpoints — Agent-gespeicherte Zwischenstaende.

Revision ID: 0048
Revises: 0047
"""
from alembic import op
import sqlalchemy as sa

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_type", sa.String(), nullable=False, server_default="manual"),
        sa.Column("state_summary", sa.String(), nullable=False),
        sa.Column("context_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_checkpoints_task_id", "task_checkpoints", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_checkpoints_task_id")
    op.drop_table("task_checkpoints")
