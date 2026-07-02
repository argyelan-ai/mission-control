"""migrate legacy agent_runtime='docker' to 'cli-bridge'

Revision ID: 0093
Revises: 0092
Create Date: 2026-04-28

Phase 15 Cleanup — Historical 'docker' designation on Sparky + FreeCode is
deprecated. Per ADR-027 only 'cli-bridge' agents support the universal
runtime switch (Anthropic Cloud / vLLM / LM Studio / Ollama). Sparky and
FreeCode are functionally cli-bridge agents — they run in mc-agent-base
containers via the same compose-rendered pipeline as Rex/Davinci/etc., they
just happen to be bound to a non-Anthropic runtime.

Keeping them as agent_runtime='docker' meant the switch service rejected
them with AgentNotSwitchableError and the docker-aware code paths needed
parallel whitelists. This migration consolidates them onto 'cli-bridge'
so a single code path and a single UI affordance handles all switchable
agents.

Idempotent: only updates rows still on the legacy value.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE agents SET agent_runtime = 'cli-bridge' WHERE agent_runtime = 'docker'"
    )


def downgrade() -> None:
    # Best-effort restore: only revert agents that have no managed runtime_id
    # (i.e. were never explicitly bound to a vllm/lmstudio runtime). Agents
    # that received a runtime_id post-migration stay on cli-bridge — they are
    # de-facto cli-bridge under the new model.
    op.execute(
        "UPDATE agents SET agent_runtime = 'docker' "
        "WHERE name IN ('Sparky', 'FreeCode') AND runtime_id IS NULL"
    )
