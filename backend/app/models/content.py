import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class ContentPipeline(SQLModel, table=True):
    """Content-Pipeline: Multi-Stage Content-Erstellung (Blog, Social, etc.)."""
    __tablename__ = "content_pipelines"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    board_id: uuid.UUID = Field(foreign_key="boards.id", index=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", nullable=True)

    title: str
    content_type: str = "blog"  # blog|social|newsletter|video|docs|linkedin_video
    status: str = "idea"  # idea|research|writing|review|approved|published|script|design|render_draft|render_final|post_ready
    brief: str | None = None  # Was soll geschrieben werden

    # Content-Felder
    research_summary: str | None = None  # Recherche-Ergebnis
    draft_content: str | None = None  # Aktueller Draft
    review_notes: str | None = None  # Review-Feedback
    final_content: str | None = None  # Finaler Content

    # Research-Verweis (optional: Link zu einer Research-Session)
    research_id: uuid.UUID | None = Field(default=None, nullable=True)

    # Agent-Zuweisungen (pro Stage)
    research_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    writing_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )
    review_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True
    )

    # Discord Channels (JSON: {"research": "channel_id", "writing": "...", ...})
    discord_channels: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    # Publishing
    published_url: str | None = None
    published_platform: str | None = None  # linkedin|twitter|blog|medium|etc
    published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # News-Bridge Fields (filled by News AI Worker)
    source_url: str | None = None  # Original article URL
    source_name: str | None = None  # e.g., "TechCrunch", "arXiv"
    ai_score: float | None = None  # 0-10 post-worthiness score
    ai_tags: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    rss_source_id: uuid.UUID | None = Field(
        default=None, foreign_key="news_sources.id", nullable=True, index=True
    )

    # LinkedIn Video Fields
    script_md: str | None = None  # Das geschriebene Skript (Markdown)
    design_md_path: str | None = None  # Pfad zur DESIGN.md Datei
    video_draft_path: str | None = None  # Pfad zum Draft-Video (MP4)
    video_final_path: str | None = None  # Pfad zum Final-Video (MP4)
    linkedin_post_text: str | None = None  # Der LinkedIn-Post-Text
    linkedin_posted_url: str | None = None  # URL des geposteten LinkedIn-Posts
    linkedin_credential_id: uuid.UUID | None = Field(
        default=None, foreign_key="credentials.id", nullable=True
    )

    # Viral-Shorts Pipeline (migration 0105). pipeline_kind discriminates rows:
    #   "article" (default, existing behavior) vs "viral_short" (new pipeline).
    # viral_metadata schema: { hook_used, dimensions_snapshot, voice_id, soul_id,
    #   render_specs, news_article_id, trend_signal_ids[] }
    # captions_per_platform: { tiktok: str, youtube_shorts: str, linkedin: str,
    #   hashtags: str[] }
    pipeline_kind: str = Field(default="article", index=True)  # article|viral_short
    auto_publish: bool = Field(default=False)
    mp4_path: str | None = None  # Final 9:16 MP4 path on host (under deliverables/)
    viral_metadata: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    captions_per_platform: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=datetime.utcnow),
    )
