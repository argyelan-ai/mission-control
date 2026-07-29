"""threads.slack_thread_ts — 1:1 Slack thread <-> MC thread

Slack's counterpart to 0166 (telegram_topic_id). A Slack "room" is a message
thread inside the default channel, identified by the parent message's `ts`
("1753699200.001900"). It is a STRING, not a number: leading/trailing zeros
carry meaning and Slack rejects a reformatted value.

Revision ID: 0170_thread_slack_thread_ts
Revises: 0169_merge_heads
"""
import sqlalchemy as sa
from alembic import op

revision = "0170_thread_slack_thread_ts"
down_revision = "0169_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("slack_thread_ts", sa.String(), nullable=True))
    # Same shape as uq_threads_telegram_topic_id: one Slack thread belongs to
    # exactly one MC thread; NULL stays free so unmapped threads are unaffected.
    op.create_unique_constraint(
        "uq_threads_slack_thread_ts", "threads", ["slack_thread_ts"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_threads_slack_thread_ts", "threads", type_="unique")
    op.drop_column("threads", "slack_thread_ts")
