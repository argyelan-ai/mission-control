"""Agent runtime field (openclaw | claude-code | manual).

Revision ID: 0031
Revises: 0030
"""

from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("agent_runtime", sa.String(), server_default="openclaw", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("agents", "agent_runtime")
