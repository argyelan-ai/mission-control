"""0100 — Extend ContentPipeline for News Bridge + News Source Board Link

Revision ID: 0100
Revises: 0099
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0100"
down_revision = "0099_news_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. content_pipelines: News-Bridge Felder ────────────────────────────────
    op.add_column("content_pipelines", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column("content_pipelines", sa.Column("source_name", sa.Text(), nullable=True))
    op.add_column("content_pipelines", sa.Column("ai_score", sa.Float(), nullable=True))
    op.add_column("content_pipelines", sa.Column("ai_tags", sa.JSON(), nullable=True))
    op.add_column(
        "content_pipelines",
        sa.Column("rss_source_id", sa.Uuid(), sa.ForeignKey("news_sources.id"), nullable=True, index=True),
    )

    # ── 2. news_sources: board_id (wo landen die Tasks?) ──────────────────────
    op.add_column(
        "news_sources",
        sa.Column("board_id", sa.Uuid(), sa.ForeignKey("boards.id"), nullable=True, index=True),
    )

    # ── 3. news_articles: has_pipeline (Deduplizierung) ─────────────────────────
    op.add_column("news_articles", sa.Column("has_pipeline", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("content_pipelines", "source_url")
    op.drop_column("content_pipelines", "source_name")
    op.drop_column("content_pipelines", "ai_score")
    op.drop_column("content_pipelines", "ai_tags")
    op.drop_column("content_pipelines", "rss_source_id")
    op.drop_column("news_sources", "board_id")
    op.drop_column("news_articles", "has_pipeline")
