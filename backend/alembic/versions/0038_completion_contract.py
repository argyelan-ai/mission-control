"""Completion Contract — report-back Verpflichtung am Task.

Revision ID: 0038
Revises: 0037
"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("report_back_required", sa.Boolean(), nullable=True, server_default="false"))
    op.add_column("tasks", sa.Column("report_back_channel", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("report_back_chat_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("report_back_requirements", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("report_back_status", sa.String(), nullable=True, server_default="none"))


def downgrade() -> None:
    op.drop_column("tasks", "report_back_status")
    op.drop_column("tasks", "report_back_requirements")
    op.drop_column("tasks", "report_back_chat_id")
    op.drop_column("tasks", "report_back_channel")
    op.drop_column("tasks", "report_back_required")
