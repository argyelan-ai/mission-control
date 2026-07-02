"""Switch task_comments.author_agent_id FK to ON DELETE SET NULL (Phase 28 prep)

Revision ID: 0121
Revises: 0120
Create Date: 2026-05-16

Initial schema (0001 line 186) declared author_agent_id as a plain
ForeignKey('agents.id') with no ondelete clause — meaning NO ACTION
in Postgres, which would block any DELETE FROM agents that left
orphaned comments. Phase 28 (Henry-Sunset, migration 0122) needs to
delete Henry's row while preserving comments, so this migration
relaxes the constraint to SET NULL. The sibling FK activity_events.
agent_id (0001 line 271) is *already* SET NULL, so no change needed
there.

Downgrade re-creates the original strict FK (NO ACTION). Failing
rows must be back-filled first — not an operational concern since
Phase 28 will never roll back in production. CI rollback tests pass
by re-establishing the strict FK after the data has been restored
via 0122 downgrade.

Per CONTEXT.md D-06.
"""
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

# In Postgres, the auto-generated FK name follows the pattern
# "{table}_{column}_fkey" because 0001 line 186 did not pass an
# explicit name to sa.ForeignKey().
_FK_NAME = "task_comments_author_agent_id_fkey"


def upgrade() -> None:
    with op.batch_alter_table("task_comments") as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.create_foreign_key(
            _FK_NAME,
            "agents",
            ["author_agent_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("task_comments") as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.create_foreign_key(
            _FK_NAME,
            "agents",
            ["author_agent_id"],
            ["id"],
        )
