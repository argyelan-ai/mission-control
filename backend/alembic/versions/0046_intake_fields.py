"""Add Operator-Intake fields for Phase 2 Structured Input.

Revision ID: 0046
Revises: 0045
"""
from alembic import op
import sqlalchemy as sa

revision = "0046"
down_revision = "0045"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("intake_mode", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("request_kind", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("desired_output", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("scope_out", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("risk_notes", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("reference_urls", sa.JSON(), nullable=True))
    op.add_column("tasks", sa.Column("reference_notes", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("approval_policy", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("autonomy_level", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("publish_allowed", sa.Boolean(), nullable=True))
    op.add_column("tasks", sa.Column("needs_browser", sa.Boolean(), nullable=True))

    op.create_check_constraint(
        "ck_tasks_intake_mode", "tasks",
        "intake_mode IS NULL OR intake_mode IN ('quick', 'structured')"
    )
    op.create_check_constraint(
        "ck_tasks_request_kind", "tasks",
        "request_kind IS NULL OR request_kind IN "
        "('code_change', 'content_create', 'research', 'browser_task', 'credential_task', 'mixed')"
    )
    op.create_check_constraint(
        "ck_tasks_approval_policy", "tasks",
        "approval_policy IS NULL OR approval_policy IN "
        "('never', 'on_plan', 'on_execution', 'on_publish', 'on_sensitive_action', 'always')"
    )
    op.create_check_constraint(
        "ck_tasks_autonomy_level", "tasks",
        "autonomy_level IS NULL OR autonomy_level IN "
        "('advise_only', 'draft_only', 'execute_low_risk', 'execute_with_approval_on_risk', 'manual_dispatch_required')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_autonomy_level", "tasks")
    op.drop_constraint("ck_tasks_approval_policy", "tasks")
    op.drop_constraint("ck_tasks_request_kind", "tasks")
    op.drop_constraint("ck_tasks_intake_mode", "tasks")
    op.drop_column("tasks", "needs_browser")
    op.drop_column("tasks", "publish_allowed")
    op.drop_column("tasks", "autonomy_level")
    op.drop_column("tasks", "approval_policy")
    op.drop_column("tasks", "reference_notes")
    op.drop_column("tasks", "reference_urls")
    op.drop_column("tasks", "risk_notes")
    op.drop_column("tasks", "scope_out")
    op.drop_column("tasks", "desired_output")
    op.drop_column("tasks", "request_kind")
    op.drop_column("tasks", "intake_mode")
