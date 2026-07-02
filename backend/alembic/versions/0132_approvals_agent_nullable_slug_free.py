"""approvals.agent_id nullable + archivierte Board-Slugs freigeben

Revision ID: 0132
Revises: 0131
Create Date: 2026-07-02

Zwei Funde aus dem Demo-Seed-Testlauf (2026-07-02):

1. approvals.agent_id war NOT NULL, aber der Watchdog erstellt
   review_stuck-Approvals mit ``agent_id=task.assigned_agent_id`` — bei
   Tasks ohne Agent (jeder Fresh-Install mit unassigned Review-Task nach
   3h) crashte damit JEDER Watchdog-Tick in einer Endlosschleife
   (Commit schlaegt fehl -> Redis-Dedup wird nie gesetzt -> Retry).

2. delete_board() ist ein Soft-Delete (is_archived=True), boards.slug
   ist aber UNIQUE — archivierte Boards blockierten ihren Slug fuer
   immer (Neuanlage -> 500). Der Router benennt ab jetzt beim
   Archivieren um; diese Migration zieht Bestands-Leichen nach.
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
    # agent_id wieder NOT NULL zu machen wuerde bestehende NULL-Approvals
    # verletzen; Slug-Umbenennung ist nicht sinnvoll umkehrbar. No-op.
    pass
