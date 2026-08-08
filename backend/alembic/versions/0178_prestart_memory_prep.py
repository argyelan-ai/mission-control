"""Pre-start memory prep + declarative recipe environment (PR 8).

Both columns exist because of the same live session: DeepSeek V4 Flash only
came up on the Spark after two pieces of manual work MC could not express.

runtimes
  * prestart_watermark_kb — the value ``vm.min_free_kbytes`` is temporarily
    lowered to while an ``exclusive_memory`` runtime starts. On a GB10 the
    engine sizes its KV cache against MemFree as CUDA sees it, and the 5 GiB
    crash-protection watermark configured on this box in July is subtracted
    from exactly that number. NULL (the default, and what every existing row
    keeps) means "do not touch the watermark" — only drop the page cache.
    The ORIGINAL value is read before lowering and restored afterwards; this
    column is only the target, never a record of what was found.

local_recipes
  * env — engine tuning as data instead of a hand-edited compose file. The
    compose recipes render these into the ``environment:`` block of the
    ``compose.override.yaml`` they already write for the container name and
    the mc.runtime.slug label, so a re-clone or a re-deploy cannot silently
    lose the tuning that made the box work.

No backfill: NULL is the correct value for every row that exists today, and
for ``env`` the seeder is insert-only per slug, so an operator who already
deployed the sparkinfer recipe keeps their hand-tuned launch_command until
they choose to re-render it (see the PR text for that one-liner).

Revision ID: 0178_prestart_memory_prep
Revises: 0177_ssh_process_runtime
"""
import sqlalchemy as sa

from alembic import op

revision = "0178_prestart_memory_prep"
down_revision = "0177_ssh_process_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtimes", sa.Column("prestart_watermark_kb", sa.Integer(), nullable=True)
    )
    op.add_column("local_recipes", sa.Column("env", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("local_recipes", "env")
    op.drop_column("runtimes", "prestart_watermark_kb")
