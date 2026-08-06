"""local_recipes — curated registry of local models/recipes for GPU boxes.

Shop window only: what RUNS stays in runtimes.model_identifier. Rows come from
config/local-recipes.json (source_registry="builtin") and, optionally, remote
registries (settings.local_registry_sources). Timestamps are timezone-aware —
naive datetimes meeting aware ones is a recurring 500 in this codebase.

Two indexes: slug is the upsert key (unique), source_registry answers "what did
this registry bring in?" when auditing an imported source.

Revision ID: 0176_local_recipes
Revises: 0175_app_settings
"""
import sqlalchemy as sa

from alembic import op

revision = "0176_local_recipes"
down_revision = "0175_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_recipes",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("model_identifier", sa.String(length=256), nullable=False),
        sa.Column("quant", sa.String(length=32), nullable=True),
        sa.Column("est_weights_gb", sa.Float(), nullable=True),
        sa.Column("min_vram_gb", sa.Float(), nullable=True),
        sa.Column("context_len", sa.Integer(), nullable=True),
        sa.Column("arch", sa.String(length=16), server_default="any", nullable=False),
        sa.Column(
            "gb10_validated", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("recipe_ref", sa.String(length=256), nullable=True),
        sa.Column("launch_template", sa.Text(), nullable=True),
        sa.Column(
            "source_registry", sa.String(length=64), server_default="builtin", nullable=False
        ),
        sa.Column("source_url", sa.String(length=512), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_local_recipes_slug", "local_recipes", ["slug"], unique=True)
    op.create_index(
        "ix_local_recipes_source_registry", "local_recipes", ["source_registry"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_local_recipes_source_registry", table_name="local_recipes")
    op.drop_index("ix_local_recipes_slug", table_name="local_recipes")
    op.drop_table("local_recipes")
