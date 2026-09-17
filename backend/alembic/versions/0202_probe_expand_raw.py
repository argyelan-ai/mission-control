"""probe expand step — companion of the deliberately bad drop probe

Throwaway probe PR (counter-check of #618): exists ONLY to prove in CI that
test_destructive_migration_not_introduced_alongside_its_own_expand_step
executes end-to-end on GitHub's runner and goes red. Close without merging.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0202_probe_expand_raw"
down_revision = "0201_agent_op_work_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("probe_col_ci", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "probe_col_ci")
