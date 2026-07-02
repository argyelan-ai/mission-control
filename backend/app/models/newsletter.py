"""NewsletterIssue — weekly digest, top-5 stories.

Aggregated by services/newsletter_service.py from storyboards with
status=published + newsletter_block_md set, pinned + engagement-sorted.
Sent via Resend to viral_shorts_settings.newsletter_subscribers.
"""
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class NewsletterIssue(SQLModel, table=True):
    __tablename__ = "newsletter_issues"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    week_start: date = Field(index=True)
    week_end: date

    subject: str
    html_body: str
    md_body: str | None = None
    top_storyboard_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )

    status: str = Field(default="draft")  # draft | sent | failed
    sent_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    recipient_count: int = Field(default=0)
    error_message: str | None = None

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("NOW()")
        ),
    )
