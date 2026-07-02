"""task use_separate_repo

Revision ID: 0056
Revises: 0055
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("use_separate_repo", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("tasks", "use_separate_repo")
