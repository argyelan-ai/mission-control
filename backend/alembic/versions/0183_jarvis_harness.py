"""Jarvis carries harness "jarvis" so the runtime picker unlocks (ADR-074).

Jarvis is the only agent whose provider lived solely in a container env var:
agent_runtime="host", harness NULL, runtime_id NULL. Runtime-switch eligibility
is derived from harness via HOST_ADAPTERS, so with a NULL harness the agent page
shows a locked badge and there is no way to bind a provider at all.

This has to be a migration rather than an API call: AgentUpdate._validate_harness
only accepts claude|openclaude|omp, so "jarvis" cannot be set through PATCH.

Numbered 0183, not 0181: the group-chat PRs (#338/#342) claimed 0181 and 0182
while this branch was open. Two migrations with the same number are not a
naming quibble — alembic reports "Multiple head revisions" and refuses to
upgrade at all, which is exactly what happened on the first deploy attempt.

Scoped to the one row that matches all three conditions, and idempotent — an
operator who already set it by hand is left alone. runtime_id stays NULL on
purpose: the seed rows for the voice runtimes are created by the seeder during
app startup, which has not run at migration time, and the first binding is a
deliberate click rather than something a migration should decide.
"""
import sqlalchemy as sa

from alembic import op

revision = "0183_jarvis_harness"
down_revision = "0182_approval_board_nullable"
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
