"""0144 — loops.telegram_reports opt-out flag (Loops L2, ADR-051).

Revision ID: 0144
Revises: 0143
"""
import sqlalchemy as sa
from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loops",
        sa.Column(
            "telegram_reports",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("loops", "telegram_reports")
