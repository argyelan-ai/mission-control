"""planner_mode — Auto/With Planner/Direct auf Tasks.

Revision ID: 0051
Revises: 0050
"""
from alembic import op
import sqlalchemy as sa

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "planner_mode",
            sa.String(),
            nullable=False,
            server_default="auto",
        ),
    )
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT ck_planner_mode "
        "CHECK (planner_mode IN ('auto', 'with_planner', 'direct'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP CONSTRAINT ck_planner_mode")
    op.drop_column("tasks", "planner_mode")
