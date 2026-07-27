"""threads.telegram_topic_id — 1:1 Telegram forum topic <-> MC thread

Revision ID: 0166_thread_telegram_topic_id
Revises: 0165_thread_agent_id
"""
import sqlalchemy as sa
from alembic import op

revision = "0166_thread_telegram_topic_id"
down_revision = "0165_thread_agent_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("telegram_topic_id", sa.BigInteger(), nullable=True))
    # 1:1 — ein Telegram-Thema haengt an genau einem Thread. NULL bleibt frei
    # (Postgres wie SQLite behandeln NULL unter UNIQUE als distinct), damit
    # themenlose Threads (still gelaufen / Allgemein) unberuehrt bleiben.
    op.create_unique_constraint(
        "uq_threads_telegram_topic_id", "threads", ["telegram_topic_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_threads_telegram_topic_id", "threads", type_="unique")
    op.drop_column("threads", "telegram_topic_id")
