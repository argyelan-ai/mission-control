"""drop planner_mode column from tasks

Revision ID: 0071
Revises: 0070
Create Date: 2026-04-11

Context: Phase 6 (Boss autonomy overhaul) completely removed the planner
path (router, delegation guards, dispatch logic, template). The
planner_mode schema field was left in place in Phase 6 for backward-compat
reasons, but is no longer read anywhere.

Phase D cleanup: drop field + constraint.

Downgrade: re-create as nullable (NOT NOT NULL with default 'auto',
because old tasks would otherwise end up with unclean values on an
up-down-up cycle). Whoever downgrades can manually reseed planner_mode.
"""
from alembic import op
import sqlalchemy as sa


revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove constraint first, then drop the column
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_planner_mode")
    op.drop_column("tasks", "planner_mode")


def downgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "planner_mode",
            sa.String(),
            nullable=True,  # downgrade state: nullable so old rows stay valid
        ),
    )
    # no CHECK constraint on downgrade — the validation was obsolete anyway
