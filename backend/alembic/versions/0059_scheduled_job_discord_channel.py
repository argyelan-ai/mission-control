"""scheduled_job discord_channel

Revision ID: 0059
Revises: 0058
Create Date: 2026-03-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_jobs", sa.Column("discord_channel_id", sa.String(), nullable=True))
    op.add_column("scheduled_jobs", sa.Column("discord_channel_name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_jobs", "discord_channel_name")
    op.drop_column("scheduled_jobs", "discord_channel_id")
