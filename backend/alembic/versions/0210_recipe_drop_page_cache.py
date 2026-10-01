"""0210 — per-recipe opt-out of the page-cache drop at start.

The pre-start memory prep (services/host_memory_prep, PR 8) drops the box's
page cache, runs a cache dropper through the load window and may lower the
free-memory watermark — for every exclusive runtime on a GB10. That is what
vLLM needs (it sizes its KV cache from MemFree). An engine that sizes its
budget from MemAvailable INCLUDING reclaimable page cache (TensorFold) gains
nothing from it and pays for it with page migration while its weights stream
in. So a recipe can now say "leave my cache alone":

- ``local_recipes.drop_page_cache`` BOOL NOT NULL DEFAULT true — the catalog
  field (seed / registry ``drop_page_cache``).
- ``runtimes.drop_page_cache`` BOOL NOT NULL DEFAULT true — copied from the
  recipe when the switcher creates an instance; the prep reads it from there.

Both additive, default true: every existing row behaves exactly as before.
No data rows (ADR-077 rule 7).

Revision ID: 0210_recipe_drop_page_cache
Revises: 0209_drop_board_groups
"""
import sqlalchemy as sa
from alembic import op

revision = "0210_recipe_drop_page_cache"
down_revision = "0209_drop_board_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "local_recipes",
        sa.Column("drop_page_cache", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "runtimes",
        sa.Column("drop_page_cache", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("runtimes", "drop_page_cache")
    op.drop_column("local_recipes", "drop_page_cache")
