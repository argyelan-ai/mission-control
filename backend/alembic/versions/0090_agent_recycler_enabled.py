"""add agents.recycler_enabled column

Revision ID: 0090
Revises: 0089
Create Date: 2026-04-26

Phase 3 Memory Leak Root-Cause Fix (MEM-01) — per-agent override for the
claude-process recycler. NULL = follow settings.agent_recycler_enabled
(default True). True/False = explicit per-agent override.

Two-tier control surface mirrors MEM-05 (Phase 1 ACK-timeout) and Phase 2
intelligence-interval: env-var = global default, DB column = per-agent
override. See ADR-024 + docs/architecture changelog.

Additive only. Idempotent because Alembic tracks the version bookkeeping.
No DB-level DEFAULT — null literally means "follow global env-var" and
must be distinguishable from explicit False (per-agent disable).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("recycler_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "recycler_enabled")
