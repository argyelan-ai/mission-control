"""0146 — agents.pending_recreate flag (CLI-Tool-Updates).

Rolling recreate propagation: when a newer CLI-tool image is built for an
agent's harness, the runtime watcher flags bound cli-bridge agents and
force-recreates each once idle. Mirrors the pending_runtime_sync mechanic
(ADR-054) but triggers a full container recreate instead of a plain restart.

Revision ID: 0146
Revises: 0145

Renumbered to 0146 after rebase onto origin/main (0144 loop_telegram_reports and 0145 loop_budget landed from parallel sessions — collision lesson #4).
"""
import sqlalchemy as sa
from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "pending_recreate",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "pending_recreate")
