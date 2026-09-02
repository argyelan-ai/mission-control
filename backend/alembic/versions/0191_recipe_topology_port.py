"""Rezept-Umschalter — Topologie und Port am Rezept-Katalog (Vertrag 02.09.2026).

Beschluss (Betreiber, 02.09.2026): Der Rezept-Umschalter zeigt ALLE
freigegebenen Rezepte und macht sie startbar — auch Zweibox-Verbünde — als
generische Kernfunktion von Mission Control. Ein Rezept ist damit
Engine · Startbefehl · Port · Topologie (Anzahl Boxen). Der sparkrun-
Sonderweg (`uvx sparkrun list`, `switch-recipe`) entfällt; ein sparkrun-
Rezept ist nur noch ein gewöhnlicher Startbefehl.

Zwei Spalten, beide additiv und nullable, damit jede bestehende Zeile ohne
Anpassung gültig bleibt:
- `topology` JSON `{"nodes": 1|2}` — NULL = 1 (Solo), genau wie heute. Das
  Rezept legt nur die ANZAHL Boxen fest, nie die Geräte: welche Box Head und
  welche Worker ist, entscheidet die Instanz (`runtimes` / `runtime_hosts`).
- `port` INT — der Standard-Port des Rezepts. Die Oberfläche braucht ihn, um
  „Port 8000 auf dieser Box belegt durch …" ehrlich sagen zu können, ohne den
  Startbefehl zu parsen.

sa.JSON() (nicht jsonb) wie 0178 (`local_recipes.env`) — dieselbe Tabelle,
dasselbe Muster. KEINE Datenzeilen: welche Rezepte ein Betreiber hat, ist
Instanz-Sache. Die Umwandlung alter `engine=sparkrun`-Zeilen macht der
Startup (services/local_registry.repair_legacy_sparkrun_rows), nicht diese
Migration — sie braucht Rezeptdaten, die im Repo nichts verloren haben.

Revision ID: 0191_recipe_topology_port
Revises: 0190_host_device_state
"""
import sqlalchemy as sa
from alembic import op

revision = "0191_recipe_topology_port"
down_revision = "0190_host_device_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("local_recipes", sa.Column("topology", sa.JSON(), nullable=True))
    op.add_column("local_recipes", sa.Column("port", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("local_recipes", "port")
    op.drop_column("local_recipes", "topology")
