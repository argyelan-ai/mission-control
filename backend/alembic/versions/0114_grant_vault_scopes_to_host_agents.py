"""Grant vault:read + vault:write to MC-managed host agents (Boss, Voice)

The eight cli-bridge docker agents received their vault scopes via an ad-hoc
SQL UPDATE during M.3 T5 rollout (see ADR-034 + 2026-05-15 vault rollout
notes in `.planning/intel/`). Boss and Voice were missed at that point because
they run on the host (Boss = native claude CLI, Voice = xAI Grok worker in
the voice-worker container).

This migration codifies the live state so a fresh DB or restore-from-backup
provisions both agents with vault access automatically. Henry is deliberately
left untouched — it is an OpenClaw Council gateway agent, not an
MC-orchestrated worker.

Idempotent: each agent only receives the scopes if they are missing. Wrapped
in PL/pgSQL because PostgreSQL's `||` operator over JSON requires explicit
casts and a NULL guard on the scopes column.

**Note (ADR-038, 2026-05-16):** the "Voice" agent was renamed to "Jarvis"
in migration 0120. This migration keeps the historical name in its WHERE
clause: at the time 0114 runs in any replay order (fresh DB, restore from
old backup), the agent is still named "Voice" — 0120 renames afterwards.
Backups from BEFORE 0114 + ABOVE 0120 don't exist (migrations only run
forward). Do not edit the names below or restoring an old snapshot will
silently skip the scope grant.

Revision ID: 0114
Revises: 0113
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


# Historical names at time of migration (see ADR-038 note above).
_HOST_AGENTS_WITH_VAULT = ("Boss", "Voice")


def upgrade() -> None:
    # For each named agent: append "vault:read" + "vault:write" to scopes
    # unless they are already present. We coerce text → jsonb explicitly
    # because the column is `json` (not `jsonb`); jsonb supports the
    # containment + concatenation operators we need.
    op.execute(
        """
        UPDATE agents
        SET scopes = (
            (scopes::jsonb)
            || CASE WHEN (scopes::jsonb) ? 'vault:read'  THEN '[]'::jsonb ELSE '["vault:read"]'::jsonb  END
            || CASE WHEN (scopes::jsonb) ? 'vault:write' THEN '[]'::jsonb ELSE '["vault:write"]'::jsonb END
        )::json
        WHERE name IN ('Boss', 'Voice')
          AND (
              NOT (scopes::jsonb) ? 'vault:read'
              OR NOT (scopes::jsonb) ? 'vault:write'
          );
        """
    )


def downgrade() -> None:
    # Removing the scopes again uses jsonb '-' to strip individual array
    # elements. Idempotent: a missing scope is a no-op.
    op.execute(
        """
        UPDATE agents
        SET scopes = (
            ((scopes::jsonb) - 'vault:read') - 'vault:write'
        )::json
        WHERE name IN ('Boss', 'Voice');
        """
    )
