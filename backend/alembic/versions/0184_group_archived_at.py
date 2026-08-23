"""Gruppen archivierbar machen (ADR-075).

Operator-Befund 22.08.2026: „ich habe keine Möglichkeit die Gruppen zu
löschen." Es gab nur einen Endpunkt zum Entfernen von MITGLIEDERN — die Gruppe
selbst blieb für immer in der Liste stehen.

Archivieren ist die reversible Stufe: die Gruppe verschwindet aus der Liste,
bleibt aber vollständig lesbar. Bewusst ein eigenes Zeitfeld statt eines
weiteren Wertes in `status`: der Status beschreibt, was die ENGINE tut
(läuft, wartet, fertig), das Archiv beschreibt, was der OPERATOR sehen will.
Zwei verschiedene Fragen — in einer Spalte vermischt hätte eine archivierte
Gruppe ihren Ausgang vergessen.

Revision ID: 0184_group_archived_at
Revises: 0183_group_speaker_timeout_900
"""
import sqlalchemy as sa
from alembic import op

revision = "0184_group_archived_at"
down_revision = "0183_group_speaker_timeout_900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_groups",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_groups_archived_at", "agent_groups", ["archived_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_groups_archived_at", table_name="agent_groups")
    op.drop_column("agent_groups", "archived_at")
