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
    # ws_url fuer WebSocket RPC Verbindungen
    op.add_column("gateways", sa.Column("ws_url", sa.Text(), nullable=True))

    # workspace_root nullable machen (lokale Gateways brauchen keinen festen Pfad)
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
