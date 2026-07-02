"""Normalize string-encoded scopes columns to native JSON arrays

Older provisioning logic occasionally stored `agents.scopes` as a JSON-encoded
*string* containing a JSON-array (`"[\\"chat:write\\", \\"tasks:read\\"]"`)
instead of a native JSON array (`["chat:write", "tasks:read"]`). The runtime
`require_scope()` dependency tolerates both shapes by re-parsing the string,
but every PostgreSQL JSONB operation (containment `?`, concatenation `||`,
array length, etc.) fails with `cannot get array length of a scalar` because
jsonb sees a scalar string instead of an array.

This migration detects the malformed rows via `jsonb_typeof(...) = 'string'`
and unwraps them: `(scopes::jsonb #>> '{}')::jsonb` extracts the inner string
content and re-casts it as a real jsonb array. Idempotent — rows that already
hold a native array are filtered out by the WHERE clause and untouched.

Only Henry currently exhibits this shape (verified live 2026-05-15) but the
migration is shape-driven rather than name-driven so any future rows that
slip through the same legacy path are repaired automatically.

Revision ID: 0115
Revises: 0114
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `scopes::jsonb #>> '{}'` extracts the root-level value as text. When
    # the stored value is a JSON string `"[\"a\", \"b\"]"`, the extraction
    # returns the inner string `[\"a\", \"b\"]`, which we then re-cast as
    # jsonb to get a proper array. When the value is already an array, the
    # WHERE clause filters the row out.
    op.execute(
        """
        UPDATE agents
        SET scopes = ((scopes::jsonb #>> '{}')::jsonb)::json
        WHERE jsonb_typeof(scopes::jsonb) = 'string';
        """
    )


def downgrade() -> None:
    # Re-wrapping a native array back into a JSON-encoded string serves no
    # operational purpose — the malformed shape was a bug, not a feature.
    # Make downgrade explicit no-op so a future operator who runs `alembic
    # downgrade` doesn't reintroduce the JSONB-incompatible representation.
    pass
