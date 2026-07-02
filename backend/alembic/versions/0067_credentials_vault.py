"""credentials vault + task.credential_id

Revision ID: 0067
Revises: 0066
Create Date: 2026-04-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("credential_type", sa.String(), nullable=False, server_default="login"),
        sa.Column("encrypted_data", sa.Text(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_credentials_name"), "credentials", ["name"], unique=False)

    op.add_column("tasks", sa.Column("credential_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_credential_id",
        "tasks",
        "credentials",
        ["credential_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_credential_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "credential_id")
    op.drop_index(op.f("ix_credentials_name"), table_name="credentials")
    op.drop_table("credentials")
