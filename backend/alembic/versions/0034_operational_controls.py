"""Add operational controls: agent mode, task run_control + dispatch_intent.

Revision ID: 0034
Revises: 0033
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("operational_mode", sa.String(), nullable=False, server_default="active"))
    op.add_column("tasks", sa.Column("run_control", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("dispatch_intent", sa.String(), nullable=False, server_default="root"))


def downgrade() -> None:
    op.drop_column("tasks", "dispatch_intent")
    op.drop_column("tasks", "run_control")
    op.drop_column("agents", "operational_mode")
