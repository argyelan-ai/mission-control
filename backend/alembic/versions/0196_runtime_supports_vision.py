"""Vision-Fähigkeit je Runtime/Rezept — an omp-Agenten weitergeben (W3, 06.09.2026).

Verifizierter Befund (live, 06.09.2026): der Motor (GLM-5.3-Flash EXL3 auf der
Head-Box, vLLM, ``--limit-mm-per-prompt {"image":4,"video":1}``) beantwortet
Bilder korrekt über die API — aber omp im Agenten-Container bekommt von MC
eine ``models.yml`` ohne ``input:``-Liste (``docker/omp-bridge/render-omp-
config.sh``). Ohne ``input: [text, image]`` gilt das MC-Modell für omp als
text-only; sein Auflösungspfad (``s.find(u => u.input.includes("image"))``,
im omp-Binary ``/usr/local/bin/omp`` per ``grep`` nachgelesen) fällt dann auf
ein eingebautes Standard-Vision-Modell zurück, das nie konfiguriert wurde —
Ergebnis: ``401 Incorrect API key provided: sk-noauth`` gegen api.openai.com
statt gegen unsere eigene Box.

Diese Migration trägt nur die DEKLARATIVE Wahrheit — WELCHE Runtime/WELCHES
Rezept Bilder kann. Die Weitergabe an omp (``OMP_MODEL_INPUT`` →
``build_runtime_env`` → ``render-omp-config.sh`` → ``models.yml``/
``modelRoles.vision``/``images.blockImages``) ist Code, keine Migration.

ZWEI Spalten, additiv, ``NOT NULL DEFAULT false`` — jede bestehende Zeile
bleibt ohne Anpassung gültig (die Vermutung "kann keine Bilder" ist der
sichere Startzustand: omp blockt Bilder dann explizit statt sie ungefragt an
einen externen Anbieter zu schicken, siehe render-omp-config.sh):

- ``runtimes.supports_vision`` — die laufende Instanz.
- ``local_recipes.supports_vision`` — der Katalogeintrag, den der
  Rezept-Umschalter beim Start in die Rezept- UND die Slot-Zeile schreibt
  (``services.recipe_switcher.build_runtime_from_recipe`` /
  ``services.slot_runtimes.write_slot_state``).

Seed (Muster-Update, KEINE Gerätedaten — nur Namens-/Slug-Muster, siehe
ADR-077 Regel 7): Zeilen, deren ``display_name``/``model_identifier`` "Vision"
enthält, oder deren ``slug`` mit ``glm53`` beginnt, werden auf ``true``
gesetzt — GLM-5.3-Flash EXL3 fährt Vision, live bewiesen (06.09.2026, rotes
Testbild → "Rot"). Alles andere (z.B. Qwen3.8 Flash Next) bleibt ``false``.
Ein frischer/öffentlicher Checkout ohne passende Zeilen führt ein No-Op-UPDATE
aus — harmlos.

Revision ID: 0196_runtime_supports_vision
Revises: 0195_runtime_serving_since
"""
import sqlalchemy as sa
from alembic import op

revision = "0196_runtime_supports_vision"
down_revision = "0195_runtime_serving_since"
branch_labels = None
depends_on = None

_SEED_WHERE = """
    display_name ILIKE '%vision%'
    OR model_identifier ILIKE '%vision%'
    OR slug ILIKE 'glm53%'
"""


def upgrade() -> None:
    op.add_column(
        "runtimes",
        sa.Column(
            "supports_vision",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "local_recipes",
        sa.Column(
            "supports_vision",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(f"UPDATE runtimes SET supports_vision = true WHERE {_SEED_WHERE}")
    op.execute(f"UPDATE local_recipes SET supports_vision = true WHERE {_SEED_WHERE}")


def downgrade() -> None:
    op.drop_column("local_recipes", "supports_vision")
    op.drop_column("runtimes", "supports_vision")
