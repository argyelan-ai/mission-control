"""Box token ledger — hourly prompt/cached/generated deltas per host × engine.

Insights shows local tokens only from MC's own transcripts; requests sent
straight to a box's API never appear there. The runtime pulse now also books
the box's own Prometheus counter deltas (services/runtime_token_ledger.py):

  runtime_token_hours     one row per host × engine × UTC hour.
  runtime_token_counters  last booked raw counter values per host × engine,
                          so a backend restart neither double counts nor
                          loses tokens; first_seen_at = counting start.

host_id is a plain UUID (no foreign key): deleting a host keeps its history.
Constraints stand identically in the model (app/models/runtime_token.py).

Additive only: nothing else reads or writes these tables.

Revision ID: 0213_runtime_token_ledger
Revises: 0212_browser_session_lifecycle
"""
import sqlalchemy as sa
from alembic import op

revision = "0213_runtime_token_ledger"
down_revision = "0212_browser_session_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_token_hours",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("generated_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "engine", "hour", name="uq_runtime_token_hours_host_engine_hour"),
    )
    op.create_index("ix_runtime_token_hours_host_id", "runtime_token_hours", ["host_id"])
    op.create_index("ix_runtime_token_hours_hour", "runtime_token_hours", ["hour"])

    op.create_table(
        "runtime_token_counters",
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("last_prompt", sa.Float(), nullable=True),
        sa.Column("last_cached", sa.Float(), nullable=True),
        sa.Column("last_generated", sa.Float(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("host_id", "engine"),
    )


def downgrade() -> None:
    op.drop_table("runtime_token_counters")
    op.drop_index("ix_runtime_token_hours_hour", table_name="runtime_token_hours")
    op.drop_index("ix_runtime_token_hours_host_id", table_name="runtime_token_hours")
    op.drop_table("runtime_token_hours")
