"""deprecate checkpoint comment_type — migrate to progress

Revision ID: 0082
Revises: 0081
Create Date: 2026-04-20

Workstream A4 (siehe docs/superpowers/plans/2026-04-20-harness-personas-
session-handoff.md):

Consolidate progress tracking around TaskChecklistItem as the single source
of truth. TaskCheckpoint + `task_comments.comment_type='checkpoint'` are
redundant with TaskChecklistItem + `comment_type='progress'`.

This migration:
  1. Migrates every existing `checkpoint`-comment to `progress` so the data
     stays visible in the comment feed under the canonical type.
  2. Leaves the `task_checkpoints` table in place as a read-only archive;
     we drop it in a follow-up migration 3+ weeks later once Sparky and the
     Docker fleet have re-synced.

The `POST /checkpoint` endpoint is marked `410 Gone` in agent_scoped.py in
the same PR — the route stays registered for 2 releases so in-flight tasks
don't 404-crash.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE task_comments
           SET comment_type = 'progress'
         WHERE comment_type = 'checkpoint'
        """
    )


def downgrade() -> None:
    # Irreversible on purpose: the distinction between 'progress' and
    # 'checkpoint' is gone forever. Downgrade is a no-op — if you need the
    # old rows back, restore from a pre-0082 DB backup.
    pass
