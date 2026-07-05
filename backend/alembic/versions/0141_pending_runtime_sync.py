"""0141 — agents.pending_runtime_sync flag (Runtime & Model Management v1, ADR-054).

Revision ID: 0141
Revises: 0140
"""
import sqlalchemy as sa
from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "pending_runtime_sync",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "pending_runtime_sync")
