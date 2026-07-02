"""drop agents.heartbeat_md (dead code, never read by agents)

Revision ID: 0125_drop_agents_heartbeat_md
Revises: 0124_task_autonomous_telegram
Create Date: 2026-05-23 10:30:00.000000

Verified across 3 agents (Researcher/Rex/Sparky) — full history.jsonl
shows 0 reads of HEARTBEAT.md. Field was write-only from agent
perspective:
- Rendered by docker_agent_sync.py to ~/.mc/agents/*/claude-config/HEARTBEAT.md
- Persisted in DB column for UI display via GET /agents/{id}/config
- Never injected into Claude's --append-system-prompt (only SOUL.md is)

Plan: docs/superpowers/plans/2026-05-23-dispatch-message-refactor.md
Phase: 4 / Task 4.1
"""
from alembic import op
import sqlalchemy as sa


revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("agents", "heartbeat_md")


def downgrade() -> None:
    op.add_column("agents", sa.Column("heartbeat_md", sa.Text(), nullable=True))
