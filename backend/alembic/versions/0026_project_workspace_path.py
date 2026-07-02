"""Add workspace_path to projects table.

Revision ID: 0026
Revises: 0025
"""

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("workspace_path", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "workspace_path")
