"""Add dispatch_config to agents

Revision ID: 0023
Revises: 0022
Create Date: 2026-03-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("dispatch_config", sa.JSON(), nullable=True, server_default="{}"))
    op.add_column("agent_templates", sa.Column("dispatch_config", sa.JSON(), nullable=True, server_default="{}"))


def downgrade() -> None:
    op.drop_column("agents", "dispatch_config")
    op.drop_column("agent_templates", "dispatch_config")
