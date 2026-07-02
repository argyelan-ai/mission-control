"""T-1: project_config + task_checklist_items + checklist-counter

Revision ID: 0054
Revises: 0053
"""
from alembic import op
import sqlalchemy as sa

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. project_config auf projects
    op.add_column("projects", sa.Column("project_config", sa.JSON(), nullable=True))

    # 2. checklist-Zähler auf tasks (denormalisiert)
    op.add_column("tasks", sa.Column("checklist_total", sa.SmallInteger(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("checklist_done", sa.SmallInteger(), nullable=False, server_default="0"))

    # 3. task_checklist_items (neue Tabelle)
    op.create_table(
        "task_checklist_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_task_checklist_task_id", "task_checklist_items", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_checklist_task_id", "task_checklist_items")
    op.drop_table("task_checklist_items")
    op.drop_column("tasks", "checklist_done")
    op.drop_column("tasks", "checklist_total")
    op.drop_column("projects", "project_config")
