"""0108_video_performance

Revision ID: 0108
Revises: 0107
Create Date: 2026-05-09

Performance tracking for published shorts.

Polled every 6h by services/performance_poller.py from TikTok Insights API +
YouTube Data API. Each row is a snapshot — multiple rows per storyboard for
trending over time.

performance_score is a normalized 0-100 metric computed from views/engagement/
watch_time. Top performers feed back into Shakespeare's learning loop.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '0108'
down_revision: Union[str, None] = '0107'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_performance",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "storyboard_id",
            UUID(as_uuid=True),
            sa.ForeignKey("storyboards.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("platform", sa.String(length=32), nullable=False),
        # tiktok | youtube_short | linkedin
        sa.Column("external_post_id", sa.String(length=128), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("likes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("saves", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("shares", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("comments", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("watch_time_pct", sa.Float(), nullable=True),
        sa.Column(
            "polled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("performance_score", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_video_performance_platform_external",
        "video_performance",
        ["platform", "external_post_id"],
    )
    op.create_index(
        "ix_video_performance_polled_at",
        "video_performance",
        ["polled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_video_performance_polled_at", table_name="video_performance")
    op.drop_index("ix_video_performance_platform_external", table_name="video_performance")
    op.drop_table("video_performance")
