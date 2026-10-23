"""Bench entries keep the operator's row order (round-2 user test t2 SOLL-1).

Entries used to be shown and composed into the video sorted by label, so
"DeepSeek · Max" always came before "DeepSeek · Off" whatever order the
operator added the rows in. ``position`` stores the row index from the
create request; every reader sorts by it (label only breaks ties).

Existing rows are backfilled per challenge in label order — exactly the
order they are shown in today, so nothing visibly moves. Additive only;
downgrade drops the column and the readers' label tie-break takes over.

Revision ID: 0221_bench_entry_position
Revises: 0220_bench_probes
"""
import sqlalchemy as sa
from alembic import op

revision = "0221_bench_entry_position"
down_revision = "0220_bench_probes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bench_entries",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE bench_entries SET position = ranked.pos "
        "FROM (SELECT id, ROW_NUMBER() OVER "
        "(PARTITION BY challenge_id ORDER BY model_label, id) - 1 AS pos "
        "FROM bench_entries) AS ranked "
        "WHERE bench_entries.id = ranked.id"
    )


def downgrade() -> None:
    op.drop_column("bench_entries", "position")
