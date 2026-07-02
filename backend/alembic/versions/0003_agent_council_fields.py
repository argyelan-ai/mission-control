"""Add Agent Council integration fields (provisioning, Discord, workspace)

Revision ID: 0003
Revises: 0002
Create Date: 2026-02-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # Agent: provisioning fields
    op.add_column("agents", sa.Column("workspace_path", sa.String(), nullable=True))
    op.add_column("agents", sa.Column("provision_status", sa.String(), server_default="local", nullable=False))
    op.add_column("agents", sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True))

    # Agent: Discord fields
    op.add_column("agents", sa.Column("discord_channel_id", sa.String(), nullable=True))
    op.add_column("agents", sa.Column("discord_channel_name", sa.String(), nullable=True))

    # Gateway: Discord fields
    op.add_column("gateways", sa.Column("discord_guild_id", sa.String(), nullable=True))
    op.add_column("gateways", sa.Column("discord_category_id", sa.String(), nullable=True))
    op.add_column("gateways", sa.Column("discord_bot_configured", sa.Boolean(), server_default="false", nullable=False))


def downgrade():
    op.drop_column("gateways", "discord_bot_configured")
    op.drop_column("gateways", "discord_category_id")
    op.drop_column("gateways", "discord_guild_id")
    op.drop_column("agents", "discord_channel_name")
    op.drop_column("agents", "discord_channel_id")
    op.drop_column("agents", "provisioned_at")
    op.drop_column("agents", "provision_status")
    op.drop_column("agents", "workspace_path")
