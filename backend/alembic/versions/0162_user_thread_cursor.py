"""0162 — user_thread_cursor: per-user read position within a thread.

Backs `my_read_seq` in the user-side thread READ API
(GET /api/v1/tasks/{task_id}/thread) and the read-marker endpoint
(POST .../thread/read). Same composite-PK shape as agent_thread_cursor
(0158), keyed by users.id instead of agents.id.

Revision ID: 0162
Revises: 0161
"""
import sqlalchemy as sa
from alembic import op

revision = "0162"
down_revision = "0161"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_thread_cursor",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            primary_key=True,
        ),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_read_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_thread_cursor")
