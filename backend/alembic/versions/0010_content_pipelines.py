"""Add content_pipelines table for multi-stage content creation

Revision ID: 0010
Revises: 0009
Create Date: 2026-02-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_pipelines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("board_id", sa.Uuid(), sa.ForeignKey("boards.id"), nullable=False, index=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False, server_default=sa.text("'blog'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'idea'")),
        sa.Column("brief", sa.Text(), nullable=True),
        sa.Column("research_summary", sa.Text(), nullable=True),
        sa.Column("draft_content", sa.Text(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("final_content", sa.Text(), nullable=True),
        sa.Column("research_id", sa.Uuid(), nullable=True),
        sa.Column("research_agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("writing_agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("review_agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("discord_channels", sa.JSON(), nullable=True),
        sa.Column("published_url", sa.Text(), nullable=True),
        sa.Column("published_platform", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("content_pipelines")
