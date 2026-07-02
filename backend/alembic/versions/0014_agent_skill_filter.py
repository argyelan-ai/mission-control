"""Add skill_filter to agents and agent_templates.

OpenClaw Gateway skill allowlist — separate from the informational 'skills' tags.
  None = all skills (default), [] = no skills, ["x"] = only these skills.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"


def upgrade() -> None:
    op.add_column("agents", sa.Column("skill_filter", sa.JSON(), nullable=True))
    op.add_column("agent_templates", sa.Column("skill_filter", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "skill_filter")
    op.drop_column("agent_templates", "skill_filter")
