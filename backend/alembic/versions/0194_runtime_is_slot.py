"""Slot-Runtime — ``runtimes.is_slot`` als explizites Kennzeichen (ADR-078).

Beschluss (Betreiber/Lead, 05.09.2026, docs/decisions/078-slot-runtime-box-url.md):
Auf einer Head-Box hören ALLE Rezepte auf derselben URL (z.B. ``:8000``). Der
Rezept-Umschalter tauscht das Modell dahinter — ein Agent, der an der Zeile des
ALTEN Rezepts hängt, fragt danach einen Modellnamen an, den die Engine nicht
mehr serviert. Bisher half nur ein Agenten-Runtime-Switch (Container-Neustart).

Die Lösung ist EINE ankerlose „Slot"-Zeile je Head-Box, an der die Agenten
hängen. Der Drift-Wächter schreibt in diese Zeile, was die Box gerade serviert
(Modell + Kontextfenster) — der Agent zeigt also immer auf die Box, nie auf ein
Rezept.

Damit das nicht an einer Konvention hängt („kein Anker, kein Startbefehl"),
bekommt die Zeile ein EXPLIZITES Kennzeichen. Eine Konvention hätte nicht
gereicht: ``recipe_switcher.recipe_matches_runtime`` erkennt eine Instanz unter
anderem am gleichen ``model_identifier`` — und genau den trägt die Slot-Zeile
per Definition. Ohne Kennzeichen hätte sie das gerade laufende Rezept „gematcht"
und es damit unstartbar gemacht.

EINE Spalte, additiv, mit Server-Default — jede bestehende Zeile bleibt ohne
Anpassung gültig und ist ``is_slot = false``:

- ``runtimes.is_slot`` BOOLEAN NOT NULL DEFAULT false — „diese Zeile ist der
  Platzhalter für das, was die Box gerade serviert". Alle Sonderregeln hängen
  an DIESEM Feld, nicht an einem Runtime-Typ: nie Rezept-Instanz, nie Belegung,
  nie Verdrängungsopfer, nie Autostart/Auto-Recovery, Drift folgt immer.

KEINE Datenzeilen (Regel 7 ADR-077): welche Boxen ein Betreiber hat, lebt in
seiner Datenbank, nicht im Repo. Die Slot-Zeilen legt der Backend-Start an
(``services/slot_runtimes.ensure_slot_runtimes``), idempotent und generisch.

Revision ID: 0194_runtime_is_slot
Revises: 0193_p3_duo_autostart
"""
import sqlalchemy as sa
from alembic import op

revision = "0194_runtime_is_slot"
down_revision = "0193_p3_duo_autostart"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtimes",
        sa.Column(
            "is_slot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("runtimes", "is_slot")
