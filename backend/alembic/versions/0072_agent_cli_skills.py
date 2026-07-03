"""add cli_skills field to agents and agent_templates

Revision ID: 0072
Revises: 0071
Create Date: 2026-04-12

Per-agent custom skill allowlist for Docker agents.
Same semantics as cli_plugins:
  null = all skills, [] = none, ["mc-debug", "mc-tdd"] = only these.

Skills live in ~/.openclaw/skills/ and are synced as real copies
into agent claude-config/skills/.
"""

from alembic import op
import sqlalchemy as sa

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("cli_skills", sa.JSON(), nullable=True))
    op.add_column("agent_templates", sa.Column("cli_skills", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_templates", "cli_skills")
    op.drop_column("agents", "cli_skills")
