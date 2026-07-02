"""task workspace_port — eindeutiger Dev-Server Port pro Task.

Revision ID: 0052
Revises: 0051
"""
from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("workspace_port", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "workspace_port")
