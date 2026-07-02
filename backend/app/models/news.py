"""News system models for Mission Control."""
from datetime import datetime
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
import uuid as uuid_module
from sqlalchemy import Column, JSON


class NewsSource(SQLModel, table=True):
    """RSS feed sources for AI news aggregation."""
    __tablename__ = "news_sources"

    id: Optional[uuid_module.UUID] = Field(default_factory=uuid_module.uuid4, primary_key=True)
    name: str = Field(index=True)
    rss_url: str
    base_url: str
    category: str = Field(default="tech")  # tech, research, business, general
    reliability_score: float = Field(default=0.8)
    crawl_interval: int = Field(default=30)  # minutes
    is_active: bool = Field(default=True)
    last_crawled_at: Optional[datetime] = None

    # Board link: where do articles from this source land as tasks?
    board_id: Optional[uuid_module.UUID] = Field(
        default=None, foreign_key="boards.id", nullable=True, index=True
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    articles: List["NewsArticle"] = Relationship(back_populates="source")


class NewsArticle(SQLModel, table=True):
    """Aggregated news articles."""
    __tablename__ = "news_articles"

    id: Optional[uuid_module.UUID] = Field(default_factory=uuid_module.uuid4, primary_key=True)
    title: str = Field(index=True)
    summary: Optional[str] = None
    content: Optional[str] = None
    url: str = Field(index=True)
    source_id: Optional[uuid_module.UUID] = Field(default=None, foreign_key="news_sources.id")
    category: str = Field(default="general")  # llm, agent, multimodal, research, business, tools
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    published_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    image_url: Optional[str] = None
    is_featured: bool = Field(default=False)
    is_posted: bool = Field(default=False)
    post_urls: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    engagement: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    slug: str = Field(index=True)
    post_worthy_score: Optional[float] = None
    ai_summary: Optional[str] = None
    ai_tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="new")  # new, scored, published, rejected, archived
    has_pipeline: bool = Field(default=False)
    pipeline_id: Optional[uuid_module.UUID] = Field(default=None, foreign_key="content_pipelines.id")

    # AI evaluation results (stored locally, no board until manual trigger)
    ai_score: Optional[float] = None
    ai_tweet: Optional[str] = None
    ai_linkedin: Optional[str] = None
    ai_category: Optional[str] = None
    processed_at: Optional[datetime] = None

    # Viral-Shorts Pipeline (migration 0105) — 8-Dim virality score + hook variants.
    # viral_score_dimensions: { hooks, emotional_peak, opinion_bomb, revelation,
    #   conflict, quotable, story_peak, practical_value } — each 0-10
    # hook_variants: [ { hook: str, framework: str, predicted_ctr: str } ]
    viral_score_dimensions: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    viral_score_total: Optional[float] = None  # Summe / max 80
    hook_variants: Optional[list] = Field(default=None, sa_column=Column(JSON))

    # AI-generated social media post suggestions
    suggested_tweet: Optional[str] = None
    suggested_linkedin: Optional[str] = None

    # Human-approved social media posts (ready for posting later)
    approved_tweet: Optional[str] = None
    approved_linkedin: Optional[str] = None
    social_status: str = Field(default="pending")  # pending, approved, rejected

    # Publishing metadata
    published_at: Optional[datetime] = None
    published_by: Optional[uuid_module.UUID] = Field(default=None, foreign_key="users.id", nullable=True)

    # Relationships
    source: Optional[NewsSource] = Relationship(back_populates="articles")
    schedules: List["NewsPostSchedule"] = Relationship(back_populates="article")


class NewsPostSchedule(SQLModel, table=True):
    """Scheduled social media posts."""
    __tablename__ = "news_post_schedules"

    id: Optional[uuid_module.UUID] = Field(default_factory=uuid_module.uuid4, primary_key=True)
    article_id: Optional[uuid_module.UUID] = Field(default=None, foreign_key="news_articles.id")
    platform: str  # twitter, linkedin, newsletter
    scheduled_at: datetime
    status: str = Field(default="pending")  # pending, posted, failed, cancelled
    content: str  # Generated post text
    posted_at: Optional[datetime] = None
    error_log: Optional[str] = None
    external_post_id: Optional[str] = None  # Tweet ID, LinkedIn post ID
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    article: Optional[NewsArticle] = Relationship(back_populates="schedules")
