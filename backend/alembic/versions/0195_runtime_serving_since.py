"""Laufzeit-Anzeige der Bühne — ``runtimes.serving_since`` (W3, 06.09.2026).

Die Runtimes-Karte zeigt heute nur den Zustand (erreichbar/nicht erreichbar),
kein "seit wann". Diese Spalte trägt den Zeitpunkt, seit dem der Wächter die
Zeile ununterbrochen erreichbar sieht — gesetzt/gelöscht in
``runtime_watcher._probe_one`` (Übergänge nicht erreichbar/unbekannt ↔
erreichbar) und explizit neu gesetzt beim bestätigten Rezept-Start der
Slot-Zeile (``services.slot_runtimes.write_slot_state``).

EINE Spalte, additiv, nullable, kein Server-Default nötig — jede bestehende
Zeile bleibt ohne Anpassung gültig (``serving_since IS NULL`` = "der Wächter
hat noch keine erfolgreiche Probe seit dieser Migration gesehen", derselbe
harmlose Startzustand wie eine frisch angelegte Zeile).

Revision ID: 0195_runtime_serving_since
Revises: 0194_runtime_is_slot
"""
import sqlalchemy as sa
from alembic import op

revision = "0195_runtime_serving_since"
down_revision = "0194_runtime_is_slot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtimes",
        sa.Column("serving_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runtimes", "serving_since")
