"""henry scope minimization — messenger role only

Revision ID: 0083
Revises: 0082
Create Date: 2026-04-20

Workstream C3 (see docs/superpowers/plans/2026-04-20-harness-personas-
session-handoff.md):

Henry's role is narrowing to "paraphrase + forward" — the operator talks to Henry,
Henry relays a structured ask to Boss, Boss orchestrates, Henry
paraphrases the answer back. Henry no longer creates tasks, manages
agents, or writes to memory. Boss (the real orchestrator) keeps all 16
scopes.

After this migration:
  - Henry can chat and read tasks
  - Henry cannot write tasks, manage agents, write memory, or approve

Idempotent: rerun-safe — the WHERE clause matches by name. Downgrade
restores the old "Lead with all scopes" setup.
"""
from alembic import op
import sqlalchemy as sa
import json

# revision identifiers, used by Alembic.
revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


HENRY_SCOPES = ["chat:write", "tasks:read"]


def upgrade() -> None:
    # JSON literal cast works for both Postgres (JSONB) and SQLite (TEXT).
    op.execute(
        sa.text(
            "UPDATE agents SET scopes = :scopes WHERE name = 'Henry'"
        ).bindparams(
            sa.bindparam("scopes", json.dumps(HENRY_SCOPES), type_=sa.JSON()),
        )
    )


def downgrade() -> None:
    # Restore the "all scopes" default. Keep this resilient: we store null
    # (= ALL_SCOPES by convention in app.scopes) rather than try to
    # reconstruct the exact prior list.
    op.execute(
        sa.text(
            "UPDATE agents SET scopes = :scopes WHERE name = 'Henry'"
        ).bindparams(
            sa.bindparam("scopes", None, type_=sa.JSON()),
        )
    )
