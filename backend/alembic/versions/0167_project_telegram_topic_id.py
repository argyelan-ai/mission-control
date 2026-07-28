"""projects.telegram_topic_id — EIN Telegram-Thema pro Projekt

Marks Regel: ein Projekt bekommt EIN Thema, in dem alle seine Tasks reden.
threads.telegram_topic_id ist unique, dort koennten sich mehrere Task-Threads
also keine ID teilen — darum haengt die geteilte ID am Projekt.

Revision ID: 0167_project_telegram_topic_id
Revises: 0166_thread_telegram_topic_id
"""
import sqlalchemy as sa
from alembic import op

revision = "0167_project_telegram_topic_id"
down_revision = "0166_thread_telegram_topic_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("telegram_topic_id", sa.BigInteger(), nullable=True))
    # 1:1 — ein Telegram-Thema haengt an genau einem Projekt. NULL bleibt frei
    # (Postgres wie SQLite behandeln NULL unter UNIQUE als distinct), damit
    # themenlose Projekte unberuehrt bleiben.
    op.create_unique_constraint(
        "uq_projects_telegram_topic_id", "projects", ["telegram_topic_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_projects_telegram_topic_id", "projects", type_="unique")
    op.drop_column("projects", "telegram_topic_id")
