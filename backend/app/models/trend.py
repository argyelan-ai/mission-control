"""TrendSignal model — multi-source trend aggregation for viral-shorts pipeline.

Captures topic signals from external sources (X, HackerNews, Reddit, news.argyelan.ai)
that the trend-research sub-skill uses to pick what's actually moving in the AI/Tech
DACH conversation right now — not just what was published.

Lifecycle: signals are time-bounded via expires_at (trend-research clears stale
ones). Cross-source clustering (topic_cluster_id) lets the LLM phase merge
"OpenAI new model" + "GPT-6 release" into one cluster before scoring.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


# Source-Konstanten — string statt Enum, damit Migration ohne CHECK-Constraint bleibt
TREND_SOURCE_X = "x"
TREND_SOURCE_HACKERNEWS = "hackernews"
TREND_SOURCE_REDDIT = "reddit"
TREND_SOURCE_NEWS_ARGYELAN = "news_argyelan"
TREND_SOURCES = {TREND_SOURCE_X, TREND_SOURCE_HACKERNEWS, TREND_SOURCE_REDDIT, TREND_SOURCE_NEWS_ARGYELAN}


class TrendSignal(SQLModel, table=True):
    __tablename__ = "trend_signals"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source: str = Field(index=True)  # x|hackernews|reddit|news_argyelan
    topic_keyword: str = Field(index=True)
    topic_cluster_id: uuid.UUID | None = Field(default=None, nullable=True)

    engagement_score: float = Field(default=0.0)
    sample_post_text: str | None = None
    sample_post_url: str | None = None
    language: str = Field(default="en", max_length=10)  # de|en|mixed
    dach_relevance: bool = Field(default=False)

    related_news_id: uuid.UUID | None = Field(
        default=None, foreign_key="news_articles.id", nullable=True,
    )

    # og:image cache (0111): Crawler extrahiert beim Fetch und speichert URL hier;
    # Storyboard-Creation kopiert per default als image_source='extracted'
    image_url: str | None = None

    captured_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )

    # extra_metadata: flexibles JSON-Feld für source-spezifische Daten
    # (z.B. tweet_id, hn_score, reddit_subreddit, like-count, etc.)
    extra_metadata: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True),
    )


class ViralShortsSettings(SQLModel, table=True):
    """Singleton-Tabelle (id=1, CHECK constraint) — globale Pipeline-Settings."""
    __tablename__ = "viral_shorts_settings"

    id: int = Field(default=1, primary_key=True)
    auto_publish_default: bool = Field(default=False)
    auto_publish_min_score: int = Field(default=75)
    daily_count: int = Field(default=1)
    cron_expression: str = Field(default="0 8 * * *")
    cron_timezone: str = Field(default="Europe/Berlin")
    voice_id: str | None = None
    soul_id: str | None = None
    extra: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Newsletter (0110)
    newsletter_subscribers: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    newsletter_sender_email: str | None = None
    newsletter_sender_name: str | None = None
    newsletter_resend_secret_id: uuid.UUID | None = Field(default=None, nullable=True)
    # i18n default (0111): pro storyboard kann Shakespeare daraus output_languages ableiten
    default_languages: list[str] = Field(
        default_factory=lambda: ["de"],
        sa_column=Column(JSON, nullable=False, server_default='["de"]'),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )
