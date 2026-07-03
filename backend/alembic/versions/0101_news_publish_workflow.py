"""0101 — News Publish Workflow: new status values + publishing metadata

Revision ID: 0101
Revises: 0100
Create Date: 2026-05-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. news_articles: publishing metadata ─────────────────────────────────────────────────────────
    # 0099 was later extended with published_at — on fresh DBs the column
    # already exists here (CI fresh-boot E2E, 2026-07-02).
    # Idempotent via IF NOT EXISTS; existing DBs unchanged.
    op.execute(
        "ALTER TABLE news_articles "
        "ADD COLUMN IF NOT EXISTS published_at TIMESTAMP WITH TIME ZONE"
    )
    op.add_column(
        "news_articles",
        sa.Column(
            "published_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # ── 2. Migrate existing data: in_pipeline → scored ────────────────────────────────────────────────────
    op.execute("UPDATE news_articles SET status = 'scored' WHERE status = 'in_pipeline'")
    op.execute("UPDATE news_articles SET status = 'scored' WHERE status = 'posted'")
    # ai_score originated historically via create_all, not via the chain — on
    # fresh DBs the column doesn't exist here (table is empty anyway).
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='news_articles' AND column_name='ai_score') THEN
                UPDATE news_articles SET status = 'scored'
                WHERE status = 'failed' AND ai_score IS NOT NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_column("news_articles", "published_by")
    op.drop_column("news_articles", "published_at")
