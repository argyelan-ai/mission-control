"""approvals.agent_id nullable + free up archived board slugs

Revision ID: 0132
Revises: 0131
Create Date: 2026-07-02

Two findings from the demo-seed test run (2026-07-02):

1. approvals.agent_id was NOT NULL, but the watchdog creates
   review_stuck approvals with ``agent_id=task.assigned_agent_id`` — for
   tasks without an agent (every fresh install with an unassigned review
   task after 3h) this crashed EVERY watchdog tick in an infinite loop
   (commit fails -> Redis dedup never gets set -> retry).

2. delete_board() is a soft delete (is_archived=True), but boards.slug
   is UNIQUE — archived boards blocked their slug forever (recreating it
   -> 500). The router now renames boards on archive; this migration
   cleans up the existing leftovers.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE approvals ALTER COLUMN agent_id DROP NOT NULL")
    op.execute(
        """
        UPDATE boards
        SET slug = slug || '--archived-' || left(replace(id::text, '-', ''), 8)
        WHERE is_archived = true AND slug NOT LIKE '%--archived-%'
        """
    )


def downgrade() -> None:
    # Making agent_id NOT NULL again would violate existing NULL approvals;
    # the slug rename can't be meaningfully reversed. No-op.
    pass
