"""Rename the Voice agent to Jarvis (ADR-038).

The orchestrator-runtime agent that bridges the operator's spoken commands through
xAI Grok was originally created with name='Voice' — same word as the LiveKit
voice infrastructure underneath it. That conflation made every "Voice"
reference in the codebase ambiguous: persona, agent name, or infra layer?
The rename to 'Jarvis' gives the persona its own handle while leaving the
voice-* infrastructure naming intact.

Scope:

1. Rename the agent row (single targeted UUID — won't accidentally touch
   any other future "Voice" agent should one be added).
2. Rewrite historical activity_events.title strings for this agent's events
   so the audit log reads consistently going forward. The operator explicitly asked
   for the rewrite — historical accuracy yields to operator clarity here.

The PBKDF2 token in agents.agent_token_hash is unchanged; the env-var rename
(VOICE_AGENT_TOKEN → JARVIS_AGENT_TOKEN) lives in .env + docker-compose +
voice_worker code, not in this migration.

Idempotent: both UPDATEs guard with name='Voice' / title LIKE '%Voice%' so
re-running on an already-migrated DB is a no-op. The targeted agent UUID
prevents collateral damage on any other future "Voice" rows.
"""

from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None

_AGENT_ID = "156b915b-2642-4924-a16a-3d91123f9b6c"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE agents
        SET name = 'Jarvis'
        WHERE id = '{_AGENT_ID}'
          AND name = 'Voice'
        """
    )
    op.execute(
        f"""
        UPDATE activity_events
        SET title = replace(title, 'Voice', 'Jarvis')
        WHERE agent_id = '{_AGENT_ID}'
          AND title LIKE '%Voice%'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE agents
        SET name = 'Voice'
        WHERE id = '{_AGENT_ID}'
          AND name = 'Jarvis'
        """
    )
    op.execute(
        f"""
        UPDATE activity_events
        SET title = replace(title, 'Jarvis', 'Voice')
        WHERE agent_id = '{_AGENT_ID}'
          AND title LIKE '%Jarvis%'
        """
    )
