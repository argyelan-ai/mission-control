"""0111_i18n_and_images

Revision ID: 0111
Revises: 0110
Create Date: 2026-05-10

i18n DE/EN parallel-fields + image support pro Storyboard.

- storyboards: linkedin_post_md_en, twitter_thread_md_en, newsletter_block_md_en,
  languages, image_url, image_source, image_alt_text, image_prompt
- trend_signals: image_url (og:image cache vom Crawler)
- viral_shorts_settings: default_languages

Bestehende _md-Felder = DE-Version (kanonisch). EN ist parallel.
Operator schaltet im UI per Toggle.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '0111'
down_revision: Union[str, None] = '0110'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Storyboards: EN-Felder + Sprach-Liste
    op.add_column("storyboards", sa.Column("linkedin_post_md_en", sa.Text(), nullable=True))
    op.add_column("storyboards", sa.Column("twitter_thread_md_en", sa.Text(), nullable=True))
    op.add_column("storyboards", sa.Column("newsletter_block_md_en", sa.Text(), nullable=True))
    op.add_column(
        "storyboards",
        sa.Column(
            "languages",
            JSONB,
            nullable=False,
            server_default=sa.text("'[\"de\"]'::jsonb"),
        ),
    )

    # Storyboards: Image
    op.add_column("storyboards", sa.Column("image_url", sa.Text(), nullable=True))
    op.add_column("storyboards", sa.Column("image_source", sa.String(length=32), nullable=True))
    # extracted | uploaded | generated | none
    op.add_column("storyboards", sa.Column("image_alt_text", sa.String(length=512), nullable=True))
    op.add_column("storyboards", sa.Column("image_prompt", sa.Text(), nullable=True))

    # Trend Signals: og:image Cache
    op.add_column("trend_signals", sa.Column("image_url", sa.Text(), nullable=True))

    # Settings: Default-Sprachen
    op.add_column(
        "viral_shorts_settings",
        sa.Column(
            "default_languages",
            JSONB,
            nullable=False,
            server_default=sa.text("'[\"de\"]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("viral_shorts_settings", "default_languages")
    op.drop_column("trend_signals", "image_url")
    op.drop_column("storyboards", "image_prompt")
    op.drop_column("storyboards", "image_alt_text")
    op.drop_column("storyboards", "image_source")
    op.drop_column("storyboards", "image_url")
    op.drop_column("storyboards", "languages")
    op.drop_column("storyboards", "newsletter_block_md_en")
    op.drop_column("storyboards", "twitter_thread_md_en")
    op.drop_column("storyboards", "linkedin_post_md_en")
