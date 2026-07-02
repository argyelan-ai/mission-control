"""Make agents board_id, gateway_id, gateway_agent_id nullable

Revision ID: 0002
Revises: 0001
Create Date: 2026-02-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("agents", "board_id", nullable=True)
    op.alter_column("agents", "gateway_id", nullable=True)
    op.alter_column("agents", "gateway_agent_id", nullable=True)


def downgrade():
    op.alter_column("agents", "gateway_agent_id", nullable=False)
    op.alter_column("agents", "gateway_id", nullable=False)
    op.alter_column("agents", "board_id", nullable=False)
