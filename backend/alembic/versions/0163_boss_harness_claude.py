"""boss.harness = 'claude' — let the host adapter reach Boss

boss-host predates HOST_ADAPTERS and its agents row still carries harness NULL.
That is enough for runtime_propagation (it falls back to derive_harness on the
bound anthropic runtime) but NOT for a manual switch: agent_runtime_switch's
_is_host_inplace reads agent.harness directly, so with NULL the UI keeps
refusing to switch Boss even though ClaudeHostAdapter now exists.

Deliberately narrow: only a host agent whose harness is still NULL and which is
bound to an anthropic-protocol runtime is touched. An operator who has already
set a harness by hand is not overwritten, and no other agent can be caught by
this. Same shape as 0081, which backfilled the claude fleet's binding.

Revision ID: 0163
Revises: 0162
Create Date: 2026-07-25
"""
from alembic import op

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # runtime_type 'cloud' + an api.anthropic.com endpoint is how the anthropic
    # runtimes are seeded (0080); matching on the endpoint rather than the slug
    # keeps this correct if the row was renamed.
    op.execute(
        """
        UPDATE agents
           SET harness = 'claude'
          FROM runtimes
         WHERE agents.runtime_id = runtimes.id
           AND agents.agent_runtime = 'host'
           AND agents.harness IS NULL
           AND runtimes.endpoint LIKE '%api.anthropic.com%'
        """
    )


def downgrade() -> None:
    # Only revert rows this migration could have set: host + claude + anthropic
    # binding. A claude harness set for any other reason stays untouched.
    op.execute(
        """
        UPDATE agents
           SET harness = NULL
          FROM runtimes
         WHERE agents.runtime_id = runtimes.id
           AND agents.agent_runtime = 'host'
           AND agents.harness = 'claude'
           AND runtimes.endpoint LIKE '%api.anthropic.com%'
        """
    )
