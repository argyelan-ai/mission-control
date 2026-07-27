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
    # Ein Agent hat hoechstens EINEN DM-Thread mit dem Operator. Ohne diesen
    # Index macht ensure_dm_thread() ein SELECT-dann-INSERT: zwei gleichzeitige
    # Aufrufe (Telegram-Nachricht + Poll im selben Moment) legen zwei Threads an,
    # beide werden zugestellt und der Verlauf zerfaellt in zwei Haelften.
    # Partiell, damit Task-/Side-Threads (agent_id IS NULL) unberuehrt bleiben.
    op.create_index(
        "uq_threads_dm_per_agent",
        "threads",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'dm'"),
        sqlite_where=sa.text("kind = 'dm'"),
    )


def downgrade() -> None:
    op.drop_index("uq_threads_dm_per_agent", table_name="threads")
    op.drop_index("ix_threads_agent_id", table_name="threads")
    op.drop_constraint("fk_threads_agent_id", "threads", type_="foreignkey")
    op.drop_column("threads", "agent_id")
