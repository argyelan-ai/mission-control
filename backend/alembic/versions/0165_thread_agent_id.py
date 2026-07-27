"""thread.agent_id — Gespraechspartner eines DM-Threads

Revision ID: 0165_thread_agent_id
Revises: 0164
"""
import sqlalchemy as sa
from alembic import op

revision = "0165_thread_agent_id"
down_revision = "0164"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("agent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_threads_agent_id", "threads", "agents", ["agent_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_threads_agent_id", "threads", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_threads_agent_id", table_name="threads")
    op.drop_constraint("fk_threads_agent_id", "threads", type_="foreignkey")
    op.drop_column("threads", "agent_id")
