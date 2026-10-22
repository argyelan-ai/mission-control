"""Bench probes: which thinking key a served model honours (bench W0.6).

One row per (runtime, served model, serving_since) — the bench's effort chip
offers only what this probe found; a restarted engine gets a fresh row.

Additive only: no existing table changes (the requested effort and what was
sent/observed per run live in bench_entries.metrics until the W1 schema PR
moves them into columns). Downgrade drops the table; the next effort chip
re-probes.

Revision ID: 0220_bench_probes
Revises: 0219_system_update
"""
import sqlalchemy as sa
from alembic import op

revision = "0220_bench_probes"
down_revision = "0219_system_update"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bench_probes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "runtime_id",
            sa.Uuid(),
            sa.ForeignKey("runtimes.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("served_model_id", sa.Text(), nullable=False),
        sa.Column("serving_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("thinking_key", sa.String(), nullable=True),
        sa.Column("effort_param", sa.String(), nullable=True),
        sa.Column("levels", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("default_observed", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("raw", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "probed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "runtime_id", "served_model_id", "serving_since", name="uq_bench_probes_serving"
        ),
    )
    op.create_index("ix_bench_probes_runtime_id", "bench_probes", ["runtime_id"])


def downgrade() -> None:
    op.drop_index("ix_bench_probes_runtime_id", table_name="bench_probes")
    op.drop_table("bench_probes")
