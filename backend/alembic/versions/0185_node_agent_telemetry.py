"""mc-node-agent Push-Telemetrie (Fleet & Rezepte v2, Phase 1).

Beschluss (Mark, 30.08.2026): SSH-Pull-Telemetrie funktioniert nur mit
vorverdrahteten Keys — ein neuer Nutzer ohne SSH-Zugriff sieht nie Metriken.
Geräte sollen sich stattdessen selbst anmelden (Pairing-Code → Token) und
Telemetrie per HTTPS pushen (routers/nodes.py). Diese Migration legt den
Grundstein:

- `hosts` bekommt vier Spalten für den Agenten-Kanal: der Token wird NUR
  als sha256-Hash gespeichert (agent_token_hash), dazu der letzte gesehene
  Zeitpunkt, der letzte Telemetrie-Schnappschuss (json — sa.JSON(), nicht
  jsonb) und die gemeldete Agenten-Version.
- `host_pairing_codes` ist eine eigene Tabelle statt Spalten auf `hosts`,
  weil ein Code auch VOR der Host-Anlage existieren kann (host_id nullable —
  siehe models/host_pairing_code.py) und weil Codes selbst kurzlebig sind
  (15-Minuten-TTL, Einmalgebrauch) — Lebenszyklus-Semantik, die nicht auf die
  langlebige hosts-Zeile gehört.

Revision ID: 0185_node_agent_telemetry
Revises: 0184_group_archived_at
"""
import sqlalchemy as sa
from alembic import op

revision = "0185_node_agent_telemetry"
down_revision = "0184_group_archived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("agent_token_hash", sa.String(length=64), nullable=True))
    op.add_column("hosts", sa.Column("agent_last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("hosts", sa.Column("agent_telemetry", sa.JSON(), nullable=True))
    op.add_column("hosts", sa.Column("agent_version", sa.String(length=32), nullable=True))

    op.create_table(
        "host_pairing_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=True),
        sa.Column("display_name_hint", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_host_pairing_codes_code", "host_pairing_codes", ["code"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_host_pairing_codes_code", table_name="host_pairing_codes")
    op.drop_table("host_pairing_codes")

    op.drop_column("hosts", "agent_version")
    op.drop_column("hosts", "agent_telemetry")
    op.drop_column("hosts", "agent_last_seen_at")
    op.drop_column("hosts", "agent_token_hash")
