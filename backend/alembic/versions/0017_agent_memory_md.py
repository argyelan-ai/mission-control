"""Add memory_md to agents

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("memory_md", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "memory_md")
