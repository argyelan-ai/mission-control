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
enthält, oder deren ``slug``/``model_identifier`` auf die EXL3-Variante von
GLM-5.3-Flash zeigt, werden auf ``true`` gesetzt — GLM-5.3-Flash EXL3 fährt
Vision, live bewiesen (06.09.2026, rotes Testbild → "Rot"). Alles andere
(z.B. Qwen3.8 Flash Next, aber auch ``glm53-dflash-sparks`` — nur die
EXL3-Variante ist live bewiesen, DFlash bleibt ``false`` bis zum eigenen
Beweis) bleibt ``false``. Ein frischer/öffentlicher Checkout ohne passende
Zeilen führt ein No-Op-UPDATE aus — harmlos.

Review-Fund (06.09.2026): die erste Fassung traf die Slot-Zeile der laufenden
Box nicht — ihr ``model_identifier`` steht als ``GLM-5.3-Flash-EXL3`` in der
DB (mit Punkt/Bindestrichen), nicht als zusammengeschriebenes ``glm53``, und
enthält auch nicht das Wort "vision". Zwei Nachbesserungen:

1. Das Namensmuster deckt jetzt explizit ``GLM-5.3-Flash-EXL3`` ab (als
   Teilstring, ``ILIKE``, unabhängig von Gross-/Kleinschreibung) — zusätzlich
   zum Katalog-Slug ``glm53-exl3%``.
2. Ein zweiter Schritt zieht jede Slot-Zeile (``is_slot = true``) nach, deren
   ``model_identifier`` exakt mit einer bereits als vision-fähig erkannten
   Zeile DERSELBEN Box (``host_id``) übereinstimmt — unabhängig davon, ob ihr
   eigener Name/Slug auf ein Muster passt. Eine Slot-Zeile serviert immer
   genau das, was das aktuell laufende Rezept serviert; sie MUSS also dessen
   Vision-Fähigkeit übernehmen, sonst bliebe die Box nach jedem Neustart des
   Backends bis zum nächsten Rezept-Wechsel ohne Vision, obwohl sie längst
   ein vision-fähiges Modell fährt.

Revision ID: 0196_runtime_supports_vision
Revises: 0195_runtime_serving_since
"""
import sqlalchemy as sa
from alembic import op

revision = "0196_runtime_supports_vision"
down_revision = "0195_runtime_serving_since"
branch_labels = None
depends_on = None

# Nur die EXL3-Variante ist live bewiesen (06.09.2026) — ``glm53-dflash-
# sparks``/DFlash bleibt bewusst aussen vor, bis sie ihren eigenen Beweis hat.
_SEED_WHERE = """
    display_name ILIKE '%vision%'
    OR model_identifier ILIKE '%vision%'
    OR slug ILIKE 'glm53-exl3%'
    OR model_identifier ILIKE '%glm-5.3-flash-exl3%'
"""

# Eine Slot-Zeile serviert IMMER genau das, was das aktuell laufende Rezept
# auf derselben Box serviert — ihre Vision-Fähigkeit folgt also der Zeile mit
# demselben model_identifier auf demselben Host, unabhängig vom eigenen
# Namen/Slug (der bei einer Slot-Zeile "<Box> :8000" lautet, nie das Rezept
# nennt).
_SLOT_SYNC_WHERE = """
    is_slot = true
    AND supports_vision = false
    AND EXISTS (
        SELECT 1 FROM runtimes AS src
        WHERE src.host_id = runtimes.host_id
          AND src.model_identifier = runtimes.model_identifier
          AND src.supports_vision = true
          AND src.id != runtimes.id
    )
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
    op.execute(f"UPDATE runtimes SET supports_vision = true WHERE {_SLOT_SYNC_WHERE}")


def downgrade() -> None:
    op.drop_column("local_recipes", "supports_vision")
    op.drop_column("runtimes", "supports_vision")
