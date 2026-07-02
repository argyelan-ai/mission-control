"""Add comment_type to task_comments

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_comments",
        sa.Column("comment_type", sa.String(), server_default="message", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("task_comments", "comment_type")
