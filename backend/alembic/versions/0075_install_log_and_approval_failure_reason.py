"""install_log table + approval.failure_reason

Revision ID: 0075
Revises: 0074
Create Date: 2026-04-18

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # approval.failure_reason for Install-Executor errors
    op.add_column(
        "approvals",
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )

    # install_log audit trail
    op.create_table(
        "install_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("approvals.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("requester_agent_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agents.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("target_agent_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("resource_name", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("installed_version", sa.String(), nullable=True),
        sa.Column("previous_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_install_log_target_agent", "install_log", ["target_agent_id"])
    op.create_index("ix_install_log_action_type", "install_log", ["action_type"])
    op.create_index("ix_install_log_created_at", "install_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_install_log_created_at", table_name="install_log")
    op.drop_index("ix_install_log_action_type", table_name="install_log")
    op.drop_index("ix_install_log_target_agent", table_name="install_log")
    op.drop_table("install_log")
    op.drop_column("approvals", "failure_reason")
