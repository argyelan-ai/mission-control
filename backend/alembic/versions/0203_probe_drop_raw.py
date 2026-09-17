"""probe destructive migration via raw SQL — deliberately bad, must go red in CI

Throwaway probe PR (counter-check of #618): this pair (new expand + new raw-SQL
drop revising it) is exactly the shape
test_destructive_migration_not_introduced_alongside_its_own_expand_step
must catch. If this PR's Backend Tests job is red with that guard's message,
the migration guard provably EXECUTES in CI — the observational hole the green
run on #618 could not close. Close without merging.
"""
from __future__ import annotations

from alembic import op

revision = "0203_probe_drop_raw"
down_revision = "0202_probe_expand_raw"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN probe_col_ci")


def downgrade() -> None:
    pass
