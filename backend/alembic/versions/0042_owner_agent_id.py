"""Add owner_agent_id to tasks for immutable ownership tracking.

owner_agent_id tracks which agent created/delegated a task.
Unlike assigned_agent_id (which changes during review handoff/rework),
owner_agent_id is set once at creation and never changes.

Revision ID: 0042
Revises: 0041
"""

from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("owner_agent_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_owner_agent_id",
        "tasks",
        "agents",
        ["owner_agent_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_owner_agent_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "owner_agent_id")
