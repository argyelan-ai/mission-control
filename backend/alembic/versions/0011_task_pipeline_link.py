"""task_pipeline_link

Revision ID: 0011
Revises: 0010
Create Date: 2026-02-22
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("pipeline_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("pipeline_stage", sa.String(), nullable=True))
    op.create_index("ix_task_pipeline_id", "tasks", ["pipeline_id"])
    op.create_foreign_key(
        "fk_tasks_pipeline_id",
        "tasks",
        "content_pipelines",
        ["pipeline_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_pipeline_id", "tasks", type_="foreignkey")
    op.drop_index("ix_task_pipeline_id", table_name="tasks")
    op.drop_column("tasks", "pipeline_stage")
    op.drop_column("tasks", "pipeline_id")
