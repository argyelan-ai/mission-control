"""Agent runtime observability fields.

Revision ID: 0030
Revises: 0029
"""

from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("last_trigger_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agents", sa.Column("last_dispatch_error", sa.String(), nullable=True))
    op.add_column("agents", sa.Column("run_state", sa.String(), nullable=False, server_default="idle"))


def downgrade() -> None:
    op.drop_column("agents", "run_state")
    op.drop_column("agents", "last_dispatch_error")
    op.drop_column("agents", "last_trigger_at")
