"""0110_newsletter

Revision ID: 0110
Revises: 0109
Create Date: 2026-05-10

Newsletter aggregator — Sunday cron picks the top 5 storyboards of the
week, renders markdown→HTML, and sends it via Resend to the subscriber
list.

- newsletter_issues: one snapshot per week
- storyboards.pinned_for_newsletter: operator can manually flag
- viral_shorts_settings extensions: subscribers, sender_email, resend-key-secret-id
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = '0110'
down_revision: Union[str, None] = '0109'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "storyboards",
        sa.Column(
            "pinned_for_newsletter",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.add_column(
        "viral_shorts_settings",
        sa.Column(
            "newsletter_subscribers",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "viral_shorts_settings",
        sa.Column("newsletter_sender_email", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "viral_shorts_settings",
        sa.Column("newsletter_sender_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "viral_shorts_settings",
        sa.Column("newsletter_resend_secret_id", UUID(as_uuid=True), nullable=True),
    )

    op.create_table(
        "newsletter_issues",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("week_start", sa.Date(), nullable=False, index=True),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("md_body", sa.Text(), nullable=True),
        sa.Column(
            "top_storyboard_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        # draft | sent | failed
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_newsletter_issues_week", "newsletter_issues", ["week_start", "week_end"], unique=True)
    op.create_index(
        "ix_storyboards_pinned_for_newsletter",
        "storyboards",
        ["pinned_for_newsletter"],
    )


def downgrade() -> None:
    op.drop_index("ix_storyboards_pinned_for_newsletter", table_name="storyboards")
    op.drop_index("ix_newsletter_issues_week", table_name="newsletter_issues")
    op.drop_table("newsletter_issues")
    op.drop_column("viral_shorts_settings", "newsletter_resend_secret_id")
    op.drop_column("viral_shorts_settings", "newsletter_sender_name")
    op.drop_column("viral_shorts_settings", "newsletter_sender_email")
    op.drop_column("viral_shorts_settings", "newsletter_subscribers")
    op.drop_column("storyboards", "pinned_for_newsletter")
