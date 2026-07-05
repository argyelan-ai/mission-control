"""0142 — agents.harness (Harness/Provider decoupling, ADR-056).

Backfill derives the harness from the agent's current runtime binding so
existing cli-bridge agents keep their exact behaviour:
  runtime_type == "omp"                 -> "omp"
  runtime slug LIKE "anthropic-claude-%" -> "claude"
  any other bound runtime               -> "openclaude"
Host agents / agents without runtime stay NULL (not switchable anyway).

Revision ID: 0142
Revises: 0141
"""
import sqlalchemy as sa
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("harness", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE agents SET harness = 'omp'
        FROM runtimes r
        WHERE agents.runtime_id = r.id
          AND agents.agent_runtime = 'cli-bridge'
          AND r.runtime_type = 'omp'
        """
    )
    op.execute(
        """
        UPDATE agents SET harness = 'claude'
        FROM runtimes r
        WHERE agents.runtime_id = r.id
          AND agents.agent_runtime = 'cli-bridge'
          AND agents.harness IS NULL
          AND r.slug LIKE 'anthropic-claude-%'
        """
    )
    op.execute(
        """
        UPDATE agents SET harness = 'openclaude'
        FROM runtimes r
        WHERE agents.runtime_id = r.id
          AND agents.agent_runtime = 'cli-bridge'
          AND agents.harness IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("agents", "harness")
