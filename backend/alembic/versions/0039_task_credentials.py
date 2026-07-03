"""Encrypted credential field for tasks.

Tasks can now contain optional credentials (Fernet-encrypted).
Agents read them decrypted via API — never as plaintext in the DB.

Revision ID: 0039
Revises: 0038
"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("credentials_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "credentials_encrypted")
