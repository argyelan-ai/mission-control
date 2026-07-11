"""0152 — migrate MC_AGENT_TOKEN vault keys from name-derived to slug-derived.

The token vault key used to be `mc_token_{agent.name.lower()}` (spaces
preserved). That orphaned on rename and broke docker/.env.agents parsing when
a name contained a space (2026-07-11: leftover `mc_token_host testpilot`). The
new scheme keys on the stable insert-time slug: `mc_token_{agent.slug}`
(spaces→dashes, never changed on rename). Single-word agents are byte-identical
under both schemes, so only multi-word agents are rewritten.

The rename/collision logic lives in app.services.vault_key_migration
(unit-tested); this migration reads the DB, plans, and executes: deletes first
(free the target key), then renames (so a collision survivor can take it
without hitting the unique constraint on secrets.key).

Revision ID: 0152
Revises: 0151
"""
from alembic import op

from app.services.vault_key_migration import migrate_connection, revert_connection

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrate_connection(op.get_bind())


def downgrade() -> None:
    revert_connection(op.get_bind())
