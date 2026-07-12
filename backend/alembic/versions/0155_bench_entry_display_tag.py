"""0155 — bench_entries.display_tag (custom chip tag for branded videos).

Nullable free-form tag shown next to the model name in the branded
side-by-side frame chip (e.g. "OMP · DGX SPARK"). NULL falls back to the
harness-derived default in bench_studio/orchestrator.py (_entry_tag).

Revision ID: 0155
Revises: 0154
"""
import sqlalchemy as sa
from alembic import op

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bench_entries",
        sa.Column("display_tag", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bench_entries", "display_tag")
