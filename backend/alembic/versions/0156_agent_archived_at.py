"""agent archived_at lifecycle field

Revision ID: 0156
Revises: 0155
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_agents_archived_at", "agents", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_agents_archived_at", table_name="agents")
    op.drop_column("agents", "archived_at")
