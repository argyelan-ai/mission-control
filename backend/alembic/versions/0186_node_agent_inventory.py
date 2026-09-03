"""mc-node-agent Gewichte-Inventar (Nachtrag zu Fleet & Rezepte v2, Phase 1).

Beschluss (Mark, 30.08.2026, Nachtrag zum node-agent-Plan): der Agent soll
zusätzlich zur Telemetrie auch melden, welche Modell-Gewichte schon auf dem
Gerät liegen (~/models-local + HF-Cache) — Grundlage für Phase 2's
"Rezept-Modell ist schon da, kein erneuter Download"-Abgleich.

Eigene Spalten statt Wiederverwendung von agent_telemetry, weil beide
unabhängig voneinander aktualisiert werden: die Telemetrie kommt mit JEDEM
Heartbeat, das Inventar nur alle ~10 Minuten UND nur wenn sich der Hash
geändert hat (siehe scripts/node-agent/mc-node-agent.py). Ein gemeinsames Feld hätte
bei jedem Telemetrie-only-Heartbeat das letzte Inventar überschreiben
müssen (oder umgekehrt) — zwei Spalten mit eigenem *_updated_at halten die
beiden Update-Frequenzen sauber getrennt.

Revision ID: 0186_node_agent_inventory
Revises: 0185_node_agent_telemetry
"""
import sqlalchemy as sa
from alembic import op

revision = "0186_node_agent_inventory"
down_revision = "0185_node_agent_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("agent_inventory", sa.JSON(), nullable=True))
    op.add_column(
        "hosts", sa.Column("agent_inventory_updated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("hosts", "agent_inventory_updated_at")
    op.drop_column("hosts", "agent_inventory")
