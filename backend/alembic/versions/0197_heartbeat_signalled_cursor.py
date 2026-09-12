"""Separate watermark for the heartbeat's soft-interrupt signal (B1, PR #519 Rex review).

``agent_task_comment_cursor.last_seen_comment_id`` is the DELIVERY watermark
``/me/poll`` uses (``_new_comments_for_agent``/``_collect_and_ack_new_comments``,
``routers/agents.py``): it advances only AFTER a comment is actually handed to
the agent. The heartbeat's soft-interrupt channel (``_collect_heartbeat_control``)
started writing that same column on every beat, before any delivery — so a
comment could be marked "seen" by the heartbeat and then never delivered by
poll at all. Reproduced live: an operator comment written between two beats
was neither signalled (soft interrupt) nor ever returned by a subsequent poll.

One shared field, two readers who each need write authority for a different
meaning ("delivered" vs. "already signalled") is the root cause. This
migration gives the heartbeat its own column instead, on the same
(agent_id, task_id) row — additive, nullable, no existing row needs
backfilling (a NULL here just means "the heartbeat hasn't signalled anything
yet for this row", the same safe-start meaning ``last_seen_comment_id`` NULL
already carries).

Revision ID: 0197_heartbeat_signalled_cursor
Revises: 0196_runtime_supports_vision
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0197_heartbeat_signalled_cursor"
down_revision = "0196_runtime_supports_vision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_task_comment_cursor",
        sa.Column("last_signalled_comment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_task_comment_cursor", "last_signalled_comment_id")
