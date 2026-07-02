"""task_attempt_audit table — permanent audit trail for dispatch_attempt_id changes

Revision ID: 0116
Revises: 0115
Create Date: 2026-05-15

Motivation: doppelter-dispatch bug 2026-05-15 (researcher / wetter-staufen):
attempt_id rotated silently between task.created and stale_update_rejected
in ~6s. No code path was clearly responsible during 30min forensics — log
data alone was insufficient.

This table receives one row per write to tasks.dispatch_attempt_id from now
on, so the next similar incident can be diagnosed in seconds via SQL instead
of 30min of code archaeology. Helper:
app.services.dispatch_attempt_audit.set_dispatch_attempt_id (single SoT).
"""
import sqlalchemy as sa
from alembic import op


revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_attempt_audit",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_attempt", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("new_attempt", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("caller", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_task_attempt_audit_task_id_created_at",
        "task_attempt_audit",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_attempt_audit_task_id_created_at",
        table_name="task_attempt_audit",
    )
    op.drop_table("task_attempt_audit")
