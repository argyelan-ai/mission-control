"""Gruppen-Turn-Timeout 600 → 900 Sekunden (Kursänderung Gruppenchat, ADR-075).

Operator-Befund 22.08.2026: lokale Motoren brauchen bei langem Kontext lange
bis zum ersten Token, und ein Gruppen-Turn schliesst Recherche und
Werkzeug-Aufrufe ein. Ein zu knapper Deckel überspringt Agenten, die noch am
Denken sind — das kostet die Runde einen ganzen Beitrag, Warten kostet nur Zeit.

Bestehende Gruppen werden mitgezogen, ABER nur die, die exakt auf dem alten
Default 600 stehen. Ein Operator, der bewusst einen eigenen Wert gesetzt hat
(z.B. 300 oder 1200), behält ihn — seine Konfiguration wird nicht überschrieben.

Revision ID: 0183_group_speaker_timeout_900
Revises: 0182_approval_board_nullable
"""
import sqlalchemy as sa
from alembic import op

revision = "0183_group_speaker_timeout_900"
down_revision = "0182_approval_board_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_groups",
        "speaker_timeout_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default="900",
    )
    op.execute(
        "UPDATE agent_groups SET speaker_timeout_seconds = 900 "
        "WHERE speaker_timeout_seconds = 600"
    )


def downgrade() -> None:
    op.alter_column(
        "agent_groups",
        "speaker_timeout_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default="600",
    )
    op.execute(
        "UPDATE agent_groups SET speaker_timeout_seconds = 600 "
        "WHERE speaker_timeout_seconds = 900"
    )
