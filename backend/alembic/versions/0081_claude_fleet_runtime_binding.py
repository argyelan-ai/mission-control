"""bind 9 agents + boss to anthropic-claude runtimes

Revision ID: 0081
Revises: 0080
Create Date: 2026-04-20

Claude-Fleet Migration (siehe docs/superpowers/plans/2026-04-20-anthropic-
claude-fleet-migration.md):

  - Boss (agent_runtime=host)       → anthropic-claude-opus (opus-4-7)
  - Rex, Davinci, Shakespeare, FreeCode, Neo, Tester, Deployer, Researcher,
    Planner (agent_runtime=cli-bridge)
                                    → anthropic-claude-sonnet (sonnet-4-6)
  - Sparky                           → unchanged (qwen-coder-lms)
  - Henry                            → unchanged (openclaw gateway)

Setzt sowohl agents.runtime_id (FK, authoritativ für docker_agent_sync +
bootstrap) als auch agents.model (Fretext-Fallback, von dispatch.py in
manchen Pfaden genutzt), damit kein Widerspruch zwischen den zwei
Source-of-Truth-Feldern entsteht.

WICHTIG: Diese Migration ändert NICHT agent_runtime — alle 9 bleiben
'cli-bridge' (Hybrid-Architektur — Container bleiben, Binary wechselt von
openclaude zu claude). Boss bleibt 'host' (war er schon via 0074).

Reprovisioning (sync-config pro Agent → rendert settings.json mit neuem
Model in claude-config/) ist ein separater Operator-Step nach dem Image-
Deploy.
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

    # 9 Sonnet-Agents
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
    # Rollback auf ollama-cloud (glm-5.1:cloud) — vor der Migration waren
    # die 9 Agents dort via 0079 gelinkt. Boss hatte claude-opus-4-7
    # (hardcoded in start-claude.sh), aber runtime_id war vermutlich NULL —
    # Downgrade setzt es auf NULL zurück.
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
