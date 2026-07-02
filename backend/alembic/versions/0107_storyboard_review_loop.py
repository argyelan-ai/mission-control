"""0107_storyboard_review_loop

Revision ID: 0107
Revises: 0106
Create Date: 2026-05-09

Operator-as-Editor pivot: extend storyboards for the autonomous review loop.

Shakespeare proposes storyboards autonomously (status awaiting_preview),
Davinci renders silent preview (status pending_review), the operator approves or
rejects with feedback. Reject triggers Shakespeare revision (status
revision_requested) which triggers a fresh silent preview.

New columns:
- silent_preview_url, silent_preview_rendered_at
- feedback_history (JSONB array of {at, reason, feedback_text})
- proposed_by_agent_id (FK agents.id)
- reasoning_md (Markdown — argyelan_angle + creative justification)
- revision_count (int, hard-cancel after 3)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = '0107'
down_revision: Union[str, None] = '0106'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "storyboards",
        sa.Column("silent_preview_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "storyboards",
        sa.Column("silent_preview_rendered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storyboards",
        sa.Column(
            "feedback_history",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "storyboards",
        sa.Column(
            "proposed_by_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "storyboards",
        sa.Column("reasoning_md", sa.Text(), nullable=True),
    )
    op.add_column(
        "storyboards",
        sa.Column(
            "revision_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.create_index(
        "ix_storyboards_proposed_by_agent_id",
        "storyboards",
        ["proposed_by_agent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_storyboards_proposed_by_agent_id", table_name="storyboards")
    op.drop_column("storyboards", "revision_count")
    op.drop_column("storyboards", "reasoning_md")
    op.drop_column("storyboards", "proposed_by_agent_id")
    op.drop_column("storyboards", "feedback_history")
    op.drop_column("storyboards", "silent_preview_rendered_at")
    op.drop_column("storyboards", "silent_preview_url")
