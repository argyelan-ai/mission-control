"""Jarvis carries harness "jarvis" so the runtime picker unlocks (ADR-074).

Jarvis is the only agent whose provider lived solely in a container env var:
agent_runtime="host", harness NULL, runtime_id NULL. Runtime-switch eligibility
is derived from harness via HOST_ADAPTERS, so with a NULL harness the agent page
shows a locked badge and there is no way to bind a provider at all.

This has to be a migration rather than an API call: AgentUpdate._validate_harness
only accepts claude|openclaude|omp, so "jarvis" cannot be set through PATCH.

Scoped to the one row that matches all three conditions, and idempotent — an
operator who already set it by hand is left alone. runtime_id stays NULL on
purpose: the seed rows for the voice runtimes are created by the seeder during
app startup, which has not run at migration time, and the first binding is a
deliberate click rather than something a migration should decide.
"""
import sqlalchemy as sa

from alembic import op

revision = "0181_jarvis_harness"
down_revision = "0180_host_tailscale_address"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agents
               SET harness = 'jarvis'
             WHERE slug = 'jarvis'
               AND agent_runtime = 'host'
               AND harness IS DISTINCT FROM 'jarvis'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agents
               SET harness = NULL
             WHERE slug = 'jarvis'
               AND harness = 'jarvis'
            """
        )
    )
