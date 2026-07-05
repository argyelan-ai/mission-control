"""0147 — agents.pending_recreate flag (CLI-Tool-Updates).

Rolling recreate propagation: when a newer CLI-tool image is built for an
agent's harness, the runtime watcher flags bound cli-bridge agents and
force-recreates each once idle. Mirrors the pending_runtime_sync mechanic
(ADR-054) but triggers a full container recreate instead of a plain restart.

Revision ID: 0147
Revises: 0146

Renumbered twice (0145→0146→0147): parallel sessions merged 0144/0145 and later 0146_runtime_autostart while this branch was in flight — migration-number collisions #4 and #5.
"""
import sqlalchemy as sa
from alembic import op

revision = "0147"
down_revision = "0146"
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
