"""Add delegation contract fields to tasks.

Structured fields for Task Design Contracts:
- delegation_type: code_change | visual_proof | credential_bound | review
- branch_name, target_url, acceptance_criteria, requires_auth, source_task_id

Revision ID: 0043
Revises: 0042
"""

from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("delegation_type", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("branch_name", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("target_url", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("acceptance_criteria", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("requires_auth", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("tasks", sa.Column("source_task_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_source_task_id", "tasks", "tasks",
        ["source_task_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_source_task_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "source_task_id")
    op.drop_column("tasks", "requires_auth")
    op.drop_column("tasks", "acceptance_criteria")
    op.drop_column("tasks", "target_url")
    op.drop_column("tasks", "branch_name")
    op.drop_column("tasks", "delegation_type")
