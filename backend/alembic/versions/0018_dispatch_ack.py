"""dispatch_ack_fields

Revision ID: 0018
Revises: 0017
Create Date: 2026-02-28

Neue Felder: dispatched_at, ack_at auf tasks Tabelle.
Backfill: bestehende in_progress Tasks als dispatched + acked markieren.
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True))

    # Bestehende in_progress Tasks rueckwirkend als dispatched + acked markieren
    op.execute(
        "UPDATE tasks SET dispatched_at = started_at, ack_at = started_at "
        "WHERE status = 'in_progress' AND started_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("tasks", "ack_at")
    op.drop_column("tasks", "dispatched_at")
