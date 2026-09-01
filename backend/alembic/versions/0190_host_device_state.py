"""Geräte-Steuerung — Soll-/Ist-Zustand am Host (Vertrag 01.09.2026).

Beschluss (Mark, 01.09.2026): MC-Nutzer sollen GPU-Modus und Härtung ihrer
DGX-Spark-Boxen aus der Oberfläche setzen. Der node-agent hat keinen
Eingangskanal (er redet nur nach aussen, Heartbeat alle 15 s) — deshalb
Soll-Zustand statt Fernbefehl: MC legt hier ab, wie das Gerät aussehen
soll, der Agent holt es sich mit dem Heartbeat ab und meldet den Ist zurück.

Drei Spalten statt einer:
- `agent_desired_state` ist die Soll-Seite und wird NUR vom Betreiber
  geschrieben (Setz-Endpunkt, admin-only),
- `agent_device_state` die Ist-Seite und wird NUR vom Gerät geschrieben.
  Ein gemeinsames Feld hätte bei jedem Heartbeat den Wunsch überschrieben.
- `agent_device_state_updated_at` getrennt von `agent_last_seen_at`, weil
  ein ALTER Agent zwar heartbeatet (last_seen frisch), aber nie einen Ist
  meldet — nur mit eigenem Zeitstempel kann die Ampel "frisch gemeldet" von
  "lebt, meldet aber nichts" unterscheiden.

sa.JSON() (nicht jsonb), wie 0185/0186 — dieselbe Tabelle, dasselbe Muster.
Alle Spalten nullable: bestehende Hosts bleiben unverändert gültig, und
"kein Soll gesetzt" ist der Normalfall (= Gerät wird nicht gesteuert).

Revision ID: 0190_host_device_state
Revises: 0189_runtime_hosts_topology
"""
import sqlalchemy as sa
from alembic import op

revision = "0190_host_device_state"
down_revision = "0189_runtime_hosts_topology"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("agent_desired_state", sa.JSON(), nullable=True))
    op.add_column("hosts", sa.Column("agent_device_state", sa.JSON(), nullable=True))
    op.add_column(
        "hosts",
        sa.Column("agent_device_state_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hosts", "agent_device_state_updated_at")
    op.drop_column("hosts", "agent_device_state")
    op.drop_column("hosts", "agent_desired_state")
