"""Add WebSocket RPC fields to gateways

Revision ID: 0006
Revises: 0005
Create Date: 2026-02-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ws_url for WebSocket RPC connections
    op.add_column("gateways", sa.Column("ws_url", sa.Text(), nullable=True))

    # make workspace_root nullable (local gateways don't need a fixed path)
    op.alter_column(
        "gateways",
        "workspace_root",
        existing_type=sa.String(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "gateways",
        "workspace_root",
        existing_type=sa.String(),
        nullable=False,
    )
    op.drop_column("gateways", "ws_url")
