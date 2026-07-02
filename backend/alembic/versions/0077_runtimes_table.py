"""runtimes table + agents.runtime_id FK

Revision ID: 0077
Revises: 0076
Create Date: 2026-04-19

Migrates runtime registry from backend/config/runtimes.json to the `runtimes`
DB table so users can CRUD runtimes via the UI. Adds agents.runtime_id (FK) for
per-agent runtime selection (cli-bridge agents only). ON DELETE SET NULL keeps
agents alive if a runtime is removed.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtimes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("runtime_type", sa.String(length=32), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=False),
        sa.Column("healthcheck_path", sa.String(length=128), nullable=True),
        sa.Column("model_identifier", sa.String(length=256), nullable=True),
        sa.Column("container_name", sa.String(length=128), nullable=True),
        sa.Column("lms_identifier", sa.String(length=256), nullable=True),
        sa.Column("lms_cli_path", sa.String(length=256), nullable=True),
        sa.Column("host", sa.String(length=128), nullable=True),
        sa.Column("role_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("supports_tools", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("supports_reasoning", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("supports_streaming", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("preferred_context_len", sa.Integer(), nullable=True),
        sa.Column("max_context_len", sa.Integer(), nullable=True),
        sa.Column("gpu_profile", sa.String(length=64), nullable=True),
        sa.Column("memory_notes", sa.Text(), nullable=True),
        sa.Column("startup_notes", sa.Text(), nullable=True),
        sa.Column("ui_order", sa.Integer(), server_default="999", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "api_key_secret_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secrets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
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
    )

    op.add_column(
        "agents",
        sa.Column(
            "runtime_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtimes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_agents_runtime_id", "agents", ["runtime_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_runtime_id", table_name="agents")
    op.drop_column("agents", "runtime_id")
    op.drop_table("runtimes")
