"""Rezept-Umschalter P2 — Geräterolle, Verbund-Adresse, exklusiv-Flag (Vertrag 02.09.2026).

Beschluss (Betreiber, 02.09.2026, docs/plans/2026-09-02-rezept-umschalter-p2-p3-vertrag.md):
Ein Zweibox-Rezept braucht eine Head- und eine Worker-Box. Damit die
Oberfläche im Duo-Dialog eine sinnvolle Vorbelegung zeigen kann, darf ein
Betreiber jeder Box eine Rolle geben. Und damit die Verdrängung ehrlich
bleibt, darf ein Rezept selbst sagen, ob es die Box exklusiv braucht.

Drei Spalten, alle additiv und nullable — jede bestehende Zeile bleibt ohne
Anpassung gültig:
- `hosts.role` TEXT, Werte `head` | `worker` | NULL. NUR eine Standard-
  vorgabe für den Zweibox-Fall. Bei Ein-Box-Rezepten wird sie ignoriert,
  und sie sperrt NIRGENDS etwas: eine Worker-Box darf jederzeit Head sein.
  Der Router prüft die zwei Werte; die Spalte selbst bleibt freier Text,
  weil ein CHECK-Constraint beim nächsten Rollenwert (P4: „beide") eine
  weitere Migration kosten würde.
- `hosts.fabric_ip` TEXT — die Adresse, unter der die Boxen sich
  GEGENSEITIG erreichen (Verbund-Kabel, nicht LAN und nicht Tailscale).
  Angelegt in P2, damit P3 (Duo-Start schreibt HEAD_IP/WORKER_IP) nur noch
  liest. NULL = „nimm ssh_host".
- `local_recipes.exclusive` BOOL — sagt das Rezept selbst, ob es die Box
  exklusiv belegt. NULL = die bisherige Heuristik (min_vram_gb gesetzt)
  bleibt der Fallback; das Feld ist die Wahrheit, sobald es gesetzt ist.

- `host_pairing_codes.role` / `host_pairing_codes.ssh_host` TEXT — Ergänzung
  (Chef-Entscheid 02.09.): jeder Weg, der eine Box anlegt, darf die Rolle
  mitgeben. Beim Pairing existiert der Host beim Minten des Codes oft noch
  nicht — also werden Rolle und SSH-Adresse am CODE zwischengelagert und
  beim Einlösen auf den dann angelegten Host übertragen.

KEINE Datenzeilen: welche Box welche Rolle hat, ist Instanz-Sache.

Revision ID: 0192_host_role_recipe_exclusive
Revises: 0191_recipe_topology_port
"""
import sqlalchemy as sa
from alembic import op

revision = "0192_host_role_recipe_exclusive"
down_revision = "0191_recipe_topology_port"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("role", sa.Text(), nullable=True))
    op.add_column("hosts", sa.Column("fabric_ip", sa.Text(), nullable=True))
    op.add_column("local_recipes", sa.Column("exclusive", sa.Boolean(), nullable=True))
    op.add_column("host_pairing_codes", sa.Column("role", sa.Text(), nullable=True))
    op.add_column("host_pairing_codes", sa.Column("ssh_host", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("host_pairing_codes", "ssh_host")
    op.drop_column("host_pairing_codes", "role")
    op.drop_column("local_recipes", "exclusive")
    op.drop_column("hosts", "fabric_ip")
    op.drop_column("hosts", "role")
