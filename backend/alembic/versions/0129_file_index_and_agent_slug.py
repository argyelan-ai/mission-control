"""MC Files System: file_index table + stable agents.slug (ADR-040).

- file_index: listing/search accelerator (capture-at-write + background walk).
  Bytes never come from here — only listings. See services/file_indexer.py.
- agents.slug: stable filesystem slug (partition key for ~/.mc/workspaces and
  ~/.mc/deliverables) so a rename no longer breaks existing deliverable paths.
  Backfilled from name; fs_service.agent_slug() falls back when NULL.

NOTE: chains off 0128 (model-price fix). If 0128 is not yet committed when this
ships, commit it first so the alembic history stays linear.

Revision ID: 0129
Revises: 0128
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. file_index ──────────────────────────────────────────────────────
    op.create_table(
        "file_index",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("root_key", sa.String(), nullable=False),
        sa.Column("rel_path", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_directory", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mime", sa.String(), nullable=True),
        sa.Column("mtime", sa.Float(), nullable=False, server_default="0"),
        sa.Column("agent_slug", sa.String(), nullable=True),
        sa.Column("runtime", sa.String(), nullable=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("deliverable_id", sa.Uuid(), sa.ForeignKey("task_deliverables.id"), nullable=True),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_file_index_root_key", "file_index", ["root_key"])
    op.create_index("ix_file_index_name", "file_index", ["name"])
    op.create_index("ix_file_index_agent_slug", "file_index", ["agent_slug"])
    op.create_index("ix_file_index_task_id", "file_index", ["task_id"])
    op.create_index("ix_file_index_deliverable_id", "file_index", ["deliverable_id"])
    op.create_index("ix_file_index_root_relpath", "file_index", ["root_key", "rel_path"], unique=True)

    # ── 2. agents.slug ─────────────────────────────────────────────────────
    op.add_column("agents", sa.Column("slug", sa.String(), nullable=True))
    # Backfill from name (matches the before_insert listener + fs_service.agent_slug)
    op.execute("UPDATE agents SET slug = lower(replace(name, ' ', '-')) WHERE slug IS NULL")
    op.create_index("ix_agents_slug", "agents", ["slug"])


def downgrade() -> None:
    op.drop_index("ix_agents_slug", table_name="agents")
    op.drop_column("agents", "slug")
    op.drop_index("ix_file_index_root_relpath", table_name="file_index")
    op.drop_index("ix_file_index_deliverable_id", table_name="file_index")
    op.drop_index("ix_file_index_task_id", table_name="file_index")
    op.drop_index("ix_file_index_agent_slug", table_name="file_index")
    op.drop_index("ix_file_index_name", table_name="file_index")
    op.drop_index("ix_file_index_root_key", table_name="file_index")
    op.drop_table("file_index")
