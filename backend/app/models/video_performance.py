"""VideoPerformance — snapshot of published-video metrics.

Polled every 6h by services/performance_poller.py from TikTok Insights API +
YouTube Data API. Each row is a snapshot — multiple rows per storyboard for
trending over time. performance_score is normalized 0-100, fed back into
Shakespeare's learning loop.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlmodel import Column, Field, SQLModel


class VideoPerformance(SQLModel, table=True):
    __tablename__ = "video_performance"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    storyboard_id: uuid.UUID = Field(
        foreign_key="storyboards.id", index=True, nullable=False
    )

    platform: str = Field(nullable=False)  # tiktok | youtube_short | linkedin
    external_post_id: str = Field(nullable=False)
    posted_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )

    views: int = Field(default=0)
    likes: int = Field(default=0)
    saves: int = Field(default=0)
    shares: int = Field(default=0)
    comments: int = Field(default=0)
    watch_time_pct: float | None = None

    polled_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("NOW()")
        ),
    )
    performance_score: float | None = None
