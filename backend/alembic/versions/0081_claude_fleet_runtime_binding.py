"""bind 9 agents + boss to anthropic-claude runtimes

Revision ID: 0081
Revises: 0080
Create Date: 2026-04-20

Claude-Fleet Migration (see docs/superpowers/plans/2026-04-20-anthropic-
claude-fleet-migration.md):

  - Boss (agent_runtime=host)       → anthropic-claude-opus (opus-4-7)
  - Rex, Davinci, Shakespeare, FreeCode, Neo, Tester, Deployer, Researcher,
    Planner (agent_runtime=cli-bridge)
                                    → anthropic-claude-sonnet (sonnet-4-6)
  - Sparky                           → unchanged (qwen-coder-lms)
  - Henry                            → unchanged (openclaw gateway)

Sets both agents.runtime_id (FK, authoritative for docker_agent_sync +
bootstrap) and agents.model (free-text fallback, used by dispatch.py in
some code paths), so the two source-of-truth fields never contradict
each other.

IMPORTANT: This migration does NOT change agent_runtime — all 9 stay
'cli-bridge' (hybrid architecture — containers stay, binary switches from
openclaude to claude). Boss stays 'host' (already was, via 0074).

Reprovisioning (sync-config per agent → renders settings.json with the
new model into claude-config/) is a separate operator step after the
image deploy.
"""
from alembic import op


revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


SONNET_AGENTS = [
    "Rex",
    "Davinci",
    "Shakespeare",
    "FreeCode",
    "Neo",
    "Tester",
    "Deployer",
    "Researcher",
    "Planner",
]


def upgrade() -> None:
    # Boss → Opus
    op.execute(
        """
        UPDATE agents
        SET runtime_id = runtimes.id,
            model = 'claude-opus-4-7',
            updated_at = NOW()
        FROM runtimes
        WHERE runtimes.slug = 'anthropic-claude-opus'
          AND agents.name = 'Boss'
        """
    )

    # 9 Sonnet agents
    agents_csv = ", ".join(f"'{n}'" for n in SONNET_AGENTS)
    op.execute(
        f"""
        UPDATE agents
        SET runtime_id = runtimes.id,
            model = 'claude-sonnet-4-6',
            updated_at = NOW()
        FROM runtimes
        WHERE runtimes.slug = 'anthropic-claude-sonnet'
          AND agents.name IN ({agents_csv})
        """
    )


def downgrade() -> None:
    # Rollback to ollama-cloud (glm-5.1:cloud) — before this migration the
    # 9 agents were linked there via 0079. Boss had claude-opus-4-7
    # (hardcoded in start-claude.sh), but runtime_id was probably NULL —
    # downgrade resets it back to NULL.
    op.execute(
        """
        UPDATE agents
        SET runtime_id = NULL,
            model = NULL,
            updated_at = NOW()
        WHERE name = 'Boss'
        """
    )

    agents_csv = ", ".join(f"'{n}'" for n in SONNET_AGENTS)
    op.execute(
        f"""
        UPDATE agents
        SET runtime_id = runtimes.id,
            model = 'glm-5.1:cloud',
            updated_at = NOW()
        FROM runtimes
        WHERE runtimes.slug = 'ollama-cloud'
          AND agents.name IN ({agents_csv})
        """
    )
