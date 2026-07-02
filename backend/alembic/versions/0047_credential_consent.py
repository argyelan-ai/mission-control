"""Add credential_consent for task-scoped auth approval bypass.

Revision ID: 0047
Revises: 0046
"""
from alembic import op
import sqlalchemy as sa

revision = "0047"
down_revision = "0046"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("credential_consent", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "credential_consent")
