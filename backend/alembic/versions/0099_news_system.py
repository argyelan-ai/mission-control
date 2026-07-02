"""News system tables for AI news aggregation (news.argyelan.ai)

Revision ID: 0099_news_system
Revises: 0098_deliverable_agent_nullable
Create Date: 2026-05-01

Phase: 1 — News system foundation
Creates news_sources, news_articles, news_post_schedules tables.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0099_news_system"
down_revision = "0098_deliverable_agent_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # news_sources
    op.create_table(
        "news_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False, index=True),
        sa.Column("rss_url", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False, server_default="tech"),
        sa.Column("reliability_score", sa.Float(), nullable=False, server_default="0.8"),
        sa.Column("crawl_interval", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # news_articles
    op.create_table(
        "news_articles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.String(), nullable=False, index=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("url", sa.String(), nullable=False, index=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("news_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("category", sa.String(), nullable=False, server_default="general"),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("is_featured", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_posted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("post_urls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("engagement", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("slug", sa.String(), nullable=False, index=True),
        sa.Column("post_worthy_score", sa.Float(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("ai_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
    )

    # Unique constraint on url to prevent duplicates
    op.create_unique_constraint("uq_news_articles_url", "news_articles", ["url"])

    # news_post_schedules
    op.create_table(
        "news_post_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("news_articles.id", ondelete="CASCADE"), nullable=True),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column("external_post_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # Seed default news sources using insert()
    sources_table = sa.table(
        "news_sources",
        sa.column("name", sa.String()),
        sa.column("rss_url", sa.String()),
        sa.column("base_url", sa.String()),
        sa.column("category", sa.String()),
        sa.column("reliability_score", sa.Float()),
        sa.column("crawl_interval", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        sources_table,
        [
            {"name": "TechCrunch AI", "rss_url": "https://techcrunch.com/category/artificial-intelligence/feed/", "base_url": "https://techcrunch.com", "category": "tech", "reliability_score": 0.9, "crawl_interval": 30, "is_active": True},
            {"name": "The Verge AI", "rss_url": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml", "base_url": "https://www.theverge.com", "category": "tech", "reliability_score": 0.85, "crawl_interval": 30, "is_active": True},
            {"name": "MIT Technology Review", "rss_url": "https://www.technologyreview.com/feed/", "base_url": "https://www.technologyreview.com", "category": "research", "reliability_score": 0.95, "crawl_interval": 60, "is_active": True},
            {"name": "Ars Technica AI", "rss_url": "https://arstechnica.com/tag/ai/feed/", "base_url": "https://arstechnica.com", "category": "tech", "reliability_score": 0.9, "crawl_interval": 30, "is_active": True},
            {"name": "VentureBeat AI", "rss_url": "https://venturebeat.com/category/ai/feed/", "base_url": "https://venturebeat.com", "category": "business", "reliability_score": 0.8, "crawl_interval": 30, "is_active": True},
            {"name": "Analytics India Magazine", "rss_url": "https://analyticsindiamag.com/feed/", "base_url": "https://analyticsindiamag.com", "category": "tech", "reliability_score": 0.75, "crawl_interval": 60, "is_active": True},
            {"name": "PapersWithCode", "rss_url": "https://paperswithcode.com/rss", "base_url": "https://paperswithcode.com", "category": "research", "reliability_score": 0.9, "crawl_interval": 60, "is_active": True},
            {"name": "arXiv cs.AI", "rss_url": "http://export.arxiv.org/rss/cs.AI", "base_url": "https://arxiv.org", "category": "research", "reliability_score": 0.95, "crawl_interval": 60, "is_active": True},
        ],
    )


def downgrade() -> None:
    op.drop_table("news_post_schedules")
    op.drop_table("news_articles")
    op.drop_table("news_sources")
