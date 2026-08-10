"""Pre-start memory-available wait (PR 10).

runtimes
  * prestart_min_available_kb — the MemAvailable floor a start waits for
    (up to settings.memory_prep_wait_timeout_seconds) after the page-cache/
    watermark prep from PR 8. NULL (the default, and what every existing row
    keeps) means "use the conservative default" in
    services/host_memory_prep.py — the schema has no gpu_memory_utilization
    or model-size column to derive a precise per-runtime figure from.

    Closes the gap live in the reboot test: a crash-looped engine's ~100 GB
    of NVRM allocations had not actually drained three minutes after the
    PR 8 prep reported success (page cache dropped, watermark lowered), and
    the start went ahead anyway into the same OOM. Now a start that never
    clears the threshold is aborted instead of attempted blind.

Revision ID: 0179_prestart_mem_available_wait
Revises: 0178_prestart_memory_prep
"""
import sqlalchemy as sa

from alembic import op

revision = "0179_prestart_mem_available_wait"
down_revision = "0178_prestart_memory_prep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtimes", sa.Column("prestart_min_available_kb", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("runtimes", "prestart_min_available_kb")
