"""Task workspace_path — Isolierter Arbeitspfad pro Task (Git Worktree).

Revision ID: 0050
Revises: 0049
"""
from alembic import op
import sqlalchemy as sa

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("workspace_path", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "workspace_path")
