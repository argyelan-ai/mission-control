"""0151 — agents.use_operating_card (context-economy Stage 2 opt-in flag).

Per-agent opt-in for the L1 Operating Card (backend/templates/CARD.md.j2,
<=5KB) as a replacement for the ~29KB SOUL.md --append-system-prompt. False
by default — every agent keeps getting the full SOUL.md until an operator
flips this flag (Sparky pilot, set via DB at deploy time, not by app code).
Mirrors the pending_recreate boolean pattern (migration 0147).

Revision ID: 0151
Revises: 0150
"""
import sqlalchemy as sa
from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "use_operating_card",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "use_operating_card")
