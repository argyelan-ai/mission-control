"""0109_multi_format_content

Revision ID: 0109
Revises: 0108
Create Date: 2026-05-09

Multi-Format Content-Plan pivot.

A "storyboard" is now a content-plan with up to 4 outputs:
- linkedin_post_md   — LinkedIn-Post Text (deutsch, 3-4 Sätze, mit Image)
- twitter_thread_md  — X / Twitter Thread (1-3 Tweets, JSON-newline-delimited)
- newsletter_block_md — Newsletter-Block (Markdown, aggregiert sonntags)
- video              — existing 22s short (only when topic is pillar-worthy)

`output_formats` declares which formats are active for this plan.
`format_publish_status` tracks per-format state: planned | published | skipped | failed.

Existing rows (Phase-1-Video-Only) get output_formats=['video'] for backwards compat.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '0109'
down_revision: Union[str, None] = '0108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "storyboards",
        sa.Column(
            "output_formats",
            JSONB,
            nullable=False,
            server_default=sa.text("""'["video"]'::jsonb"""),
        ),
    )
    op.add_column(
        "storyboards",
        sa.Column(
            "format_publish_status",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("storyboards", sa.Column("linkedin_post_md", sa.Text(), nullable=True))
    op.add_column("storyboards", sa.Column("twitter_thread_md", sa.Text(), nullable=True))
    op.add_column("storyboards", sa.Column("newsletter_block_md", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("storyboards", "newsletter_block_md")
    op.drop_column("storyboards", "twitter_thread_md")
    op.drop_column("storyboards", "linkedin_post_md")
    op.drop_column("storyboards", "format_publish_status")
    op.drop_column("storyboards", "output_formats")
