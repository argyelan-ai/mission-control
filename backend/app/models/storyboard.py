"""Storyboard model — Operator-as-Director architecture for viral shorts.

A Storyboard is the planned visual breakdown of a short BEFORE rendering.
The operator edits storyboards in the Director Console at /content?tab=shorts.
Davinci is dispatched to render strictly what the approved storyboard
specifies — no LLM-creative-decisions.

Lifecycle:
    draft → approved → rendering → rendered → review → published
                  └→ rejected (feedback to the operator)

Each storyboard has a 1:1 relationship with a content_pipeline row.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, text
from sqlmodel import Column, Field, SQLModel


class Storyboard(SQLModel, table=True):
    __tablename__ = "storyboards"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    content_pipeline_id: uuid.UUID = Field(
        foreign_key="content_pipelines.id", unique=True, index=True
    )

    title: str
    topic_summary: str | None = None
    topic_cluster: str | None = None  # hard_news|opinion|release|tutorial|funding
    duration_s: float = 22.0
    status: str = Field(default="draft", index=True)
    # draft|awaiting_preview|pending_review|revision_requested|approved|
    # rendering|rendered|review|published|rejected

    # Beats — JSONB array of beat objects (see beat schema in plan-storyboard.md)
    beats: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, server_default="[]")
    )

    # Voiceover
    voiceover_path: str | None = None
    alignment_path: str | None = None
    use_existing_voiceover: bool = Field(default=False)

    # Render output
    mp4_path: str | None = None
    props_json_path: str | None = None
    render_log: str | None = None

    # Operator approval
    approved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    approved_by: str | None = None
    rejection_reason: str | None = None

    # Davinci task linkage
    render_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="tasks.id", nullable=True
    )

    # Operator-as-Editor review loop (added in 0107)
    silent_preview_url: str | None = None
    silent_preview_rendered_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    feedback_history: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    proposed_by_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agents.id", nullable=True, index=True
    )
    reasoning_md: str | None = None
    revision_count: int = Field(default=0)

    # Multi-Format Pivot (added in 0109): a storyboard is a content-plan with
    # up to 4 outputs. output_formats declares which are active. Each format
    # gets a publish-state in format_publish_status: planned|published|skipped|failed.
    output_formats: list[str] = Field(
        default_factory=lambda: ["video"],
        sa_column=Column(JSON, nullable=False, server_default='["video"]'),
    )
    format_publish_status: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    linkedin_post_md: str | None = None
    twitter_thread_md: str | None = None
    newsletter_block_md: str | None = None
    pinned_for_newsletter: bool = Field(default=False, index=True)

    # i18n (0111): bestehende _md-Felder sind kanonisch DE; _en ist parallele Version
    linkedin_post_md_en: str | None = None
    twitter_thread_md_en: str | None = None
    newsletter_block_md_en: str | None = None
    languages: list[str] = Field(
        default_factory=lambda: ["de"],
        sa_column=Column(JSON, nullable=False, server_default='["de"]'),
    )

    # Image (0111): primary auto-extracted from trend.source_url (og:image),
    # secondary manual upload. image_source: extracted|uploaded|generated|none
    image_url: str | None = None
    image_source: str | None = None
    image_alt_text: str | None = None
    image_prompt: str | None = None

    # Diversity fingerprint — used by next storyboard's prompt to avoid repetition
    diversity_fingerprint: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True), nullable=False,
            server_default=text("NOW()"), onupdate=datetime.utcnow,
        ),
    )
