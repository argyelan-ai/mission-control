"""Rezept-Umschalter P3 — Zweibox-Start und Autostart je Box (Entwurf 04.09.2026).

Beschluss (Betreiber, 04.09.2026): Ein Zweibox-Rezept soll per Klick starten,
mit wählbarer Worker-Box. MC spricht dabei weiter NUR mit dem Head (ADR-077) —
das Rezept holt seinen Worker selbst dazu, gesteuert über seine eigene `.env`.
Damit MC diese `.env` schreiben kann, muss am Katalog stehen, WO sie liegt und
WELCHE Schlüssel welche Adresse bekommen. Und damit ein Ausfall nicht mehr
blind wiederbelebt wird, bekommt jede Box einen eigenen Autostart-Schalter.

Sechs Spalten, alle additiv; jede bestehende Zeile bleibt ohne Anpassung gültig:

- `local_recipes.env_file` TEXT — Pfad der Rezept-`.env` auf dem Head
  (z.B. `~/code/mein-rezept/.env`). NULL = das Rezept hat keine, dann kann MC
  für dieses Rezept keinen Zweibox-Start schreiben und sagt das.
- `local_recipes.env_map` JSON — flache Zuordnung `{"ENV_KEY": "{platzhalter}"}`.
  Bekannte Platzhalter: `{head_ip}` `{worker_ip}` (ssh_host),
  `{head_fabric_ip}` `{worker_fabric_ip}` (hosts.fabric_ip, Fallback ssh_host),
  `{head_ssh}` `{worker_ssh}` (user@ssh_host). Kein Gerätename im Katalog —
  welche Box gemeint ist, entscheidet erst der Start.
- `hosts.autostart_enabled` BOOL NOT NULL DEFAULT false — der Schalter je Box.
  AUS heisst: MC startet auf dieser Box von sich aus GAR NICHTS (ersetzt den
  `runtimes.enabled=false`-Trick, mit dem Handtests bisher vor der
  15-Minuten-Wiederbelebung geschützt werden mussten).
- `hosts.autostart_recipe_slug` TEXT — das zuletzt über den Umschalter
  gestartete Rezept (Head-Sicht). Nur DIESES Rezept wird wiederbelebt.
- `hosts.autostart_last_attempt_at` TIMESTAMPTZ / `hosts.autostart_last_result`
  TEXT — was beim letzten Versuch passiert ist, als ein Satz für die Kachel.

KEINE Datenzeilen (Regel 7 ADR-077): welche Rezepte und Boxen ein Betreiber
hat, lebt in seiner Datenbank, nicht im Repo.

Revision ID: 0193_p3_duo_autostart
Revises: 0192_host_role_recipe_exclusive
"""
import sqlalchemy as sa
from alembic import op

revision = "0193_p3_duo_autostart"
down_revision = "0192_host_role_recipe_exclusive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("local_recipes", sa.Column("env_file", sa.Text(), nullable=True))
    op.add_column("local_recipes", sa.Column("env_map", sa.JSON(), nullable=True))
    op.add_column(
        "hosts",
        sa.Column(
            "autostart_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("hosts", sa.Column("autostart_recipe_slug", sa.Text(), nullable=True))
    op.add_column(
        "hosts",
        sa.Column("autostart_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("hosts", sa.Column("autostart_last_result", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "autostart_last_result")
    op.drop_column("hosts", "autostart_last_attempt_at")
    op.drop_column("hosts", "autostart_recipe_slug")
    op.drop_column("hosts", "autostart_enabled")
    op.drop_column("local_recipes", "env_map")
    op.drop_column("local_recipes", "env_file")
