"""Add planner fields to projects + planner_messages table

Revision ID: 0009
Revises: 0008
Create Date: 2026-02-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Project: neue Felder
    op.add_column(
        "projects",
        sa.Column("project_type", sa.Text(), nullable=False, server_default=sa.text("'feature'")),
    )
    op.add_column(
        "projects",
        sa.Column("created_by", sa.Text(), nullable=False, server_default=sa.text("'user'")),
    )

    # Planner Messages Tabelle
    op.create_table(
        "planner_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("planner_messages")
    op.drop_column("projects", "created_by")
    op.drop_column("projects", "project_type")
