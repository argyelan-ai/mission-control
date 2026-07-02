"""Add auto_dispatch_enabled to boards

Revision ID: 0008
Revises: 0007
Create Date: 2026-02-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column("auto_dispatch_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("boards", "auto_dispatch_enabled")
