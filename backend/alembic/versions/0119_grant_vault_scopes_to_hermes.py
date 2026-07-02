"""Grant vault:read + vault:write to Hermes

Hermes was missed by migration 0114 (which targeted host-runtime agents
Boss + Voice) because Hermes is an MCP-bridge docker agent — neither a
cli-bridge worker (which got vault scopes via M.3 T5 SQL UPDATE) nor a
host-runtime orchestrator. After Phase D shipped (TOOLS.md + SOUL.md
vault-files docs gated on vault:write), Hermes was the only agent with
otherwise-full scopes that still rendered no vault section, so it could
neither read nor write the operator's brain.

Same 1:1 pattern as 0114 — JSON-cast for idempotent jsonb manipulation,
only appends scopes that are missing. Reversible.

Revision ID: 0119
Revises: 0118
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Append vault:read + vault:write to Hermes' scopes unless already present.
    # Mirrors the cast-dance from 0114 verbatim: agents.scopes is `json`, but
    # the containment and concat operators we need only exist on `jsonb`.
    op.execute(
        """
        UPDATE agents
        SET scopes = (
            (scopes::jsonb)
            || CASE WHEN (scopes::jsonb) ? 'vault:read'  THEN '[]'::jsonb ELSE '["vault:read"]'::jsonb  END
            || CASE WHEN (scopes::jsonb) ? 'vault:write' THEN '[]'::jsonb ELSE '["vault:write"]'::jsonb END
        )::json
        WHERE name = 'Hermes'
          AND (
              NOT (scopes::jsonb) ? 'vault:read'
              OR NOT (scopes::jsonb) ? 'vault:write'
          );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE agents
        SET scopes = (
            ((scopes::jsonb) - 'vault:read') - 'vault:write'
        )::json
        WHERE name = 'Hermes';
        """
    )
