"""Grant comms:talk to agents that hold every other scope (ADR-100).

`comms:talk` (talk across harnesses) is new in `app.scopes`. Agents with
an empty scope list already get it (empty = all scopes). Agents whose stored
list holds EVERY scope that existed before — the lead / orchestrator rows,
created from ALL_SCOPES — get it appended, so "has everything" stays true.
Every other agent keeps its list; the operator grants comms:talk per agent.

Same jsonb cast pattern as 0114 / 0119. Idempotent; downgrade removes it
from exactly those agents.

Revision ID: 0225_comms_talk_scope
Revises: 0224_bench_w1_schema
"""
from alembic import op

revision = "0225_comms_talk_scope"
down_revision = "0224_bench_w1_schema"
branch_labels = None
depends_on = None

# Every scope that existed before comms:talk (app.scopes on 2026-11-01).
_FULL_BEFORE = '["tasks:read", "tasks:write", "tasks:create", "tasks:manage", "knowledge:read", "knowledge:write", "memory:read", "memory:write", "approvals:create", "chat:write", "agents:manage", "content:submit", "heartbeat", "deploy:execute", "project:read", "project:write", "tasks:help", "credentials:read", "vault:read", "vault:write", "telegram:send"]'


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE agents
        SET scopes = ((scopes::jsonb) || '["comms:talk"]'::jsonb)::json
        WHERE scopes IS NOT NULL
          AND (scopes::jsonb) @> '{_FULL_BEFORE}'::jsonb
          AND NOT (scopes::jsonb) ? 'comms:talk';
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE agents
        SET scopes = ((scopes::jsonb) - 'comms:talk')::json
        WHERE scopes IS NOT NULL
          AND (scopes::jsonb) @> '{_FULL_BEFORE}'::jsonb;
        """
    )
