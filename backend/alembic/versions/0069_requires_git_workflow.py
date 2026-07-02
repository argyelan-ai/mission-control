"""add requires_git_workflow to agent

Revision ID: 0069
Revises: 0068
Create Date: 2026-04-07
"""
from alembic import op
import sqlalchemy as sa

revision = '0069'
down_revision = '0068'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('requires_git_workflow', sa.Boolean(), nullable=False, server_default='true'))

    # Non-Coder Agents auf False setzen
    op.execute("""
        UPDATE agents
        SET requires_git_workflow = false
        WHERE name IN ('researcher', 'shakespeare', 'rex', 'davinci', 'boss', 'henry', 'planner')
    """)


def downgrade() -> None:
    op.drop_column('agents', 'requires_git_workflow')
