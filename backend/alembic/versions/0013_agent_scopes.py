"""agents.scopes + agent_templates.scopes — Scope-based Permission System

Revision ID: 0013
Revises: 0012
Create Date: 2026-02-24
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Scopes auf agents (leere Liste = ALL_SCOPES, backward compat)
    op.add_column("agents", sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"))
    # Scopes auf agent_templates
    op.add_column("agent_templates", sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("agent_templates", "scopes")
    op.drop_column("agents", "scopes")
