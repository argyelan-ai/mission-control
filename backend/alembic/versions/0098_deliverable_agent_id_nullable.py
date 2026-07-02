"""Make task_deliverables.agent_id nullable for admin-created deliverables (HERM-11/F4)

Revision ID: 0098_deliverable_agent_nullable
Revises: 0097_per_agent_idle_timeout
Create Date: 2026-05-01

Phase 26 Plan 04 (HERM-11/F4): mc_register_deliverable MCP tool uses admin
JWT (not an agent token), so deliverables registered via MCP / admin UI
have no agent_id to attach. Make the column nullable so the new admin-
scoped POST route in tasks.py can insert without forging a synthetic
agent.

Backwards-compat: existing rows already have agent_id set; this only
relaxes the NOT NULL constraint. agent_scoped.py POST keeps requiring an
agent token and continues to populate agent_id from the token-resolved
Agent object — no behaviour change for that path.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0098_deliverable_agent_nullable"
down_revision = "0097_per_agent_idle_timeout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_deliverables") as batch:
        batch.alter_column("agent_id", existing_type=sa.dialects.postgresql.UUID(), nullable=True)


def downgrade() -> None:
    # Caveat: if rows with NULL agent_id exist, this will fail. Backfill them
    # to a sentinel agent UUID before downgrading. Safe in practice because
    # admin-created deliverables only land via the new HERM-11/F4 route — if
    # nobody used it, downgrade is clean.
    with op.batch_alter_table("task_deliverables") as batch:
        batch.alter_column("agent_id", existing_type=sa.dialects.postgresql.UUID(), nullable=False)
