"""0142 — tasks.e2e_test_required flag (Human-simulating E2E toggle).

Operator-requested E2E gate: review approval routes the task through
user_test (tester agent, Playwright MCP) even without subtasks.

Revision ID: 0142
Revises: 0141
"""
import sqlalchemy as sa
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("e2e_test_required", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "e2e_test_required")
