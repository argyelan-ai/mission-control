"""Fresh-install schema repair — model↔chain drift (additive only)

Revision ID: 0131
Revises: 0130
Create Date: 2026-07-02

Der CI fresh-boot E2E (Kette 0001→0130 auf leerer DB) deckte auf, dass
sieben Model-Spalten historisch via App-``create_all``/Hand-SQL entstanden
sind und in KEINER Migration existieren. Bestands-DBs haben sie längst
(dort ist alles hier ein No-op via IF NOT EXISTS); frische Installationen
brauchen sie, sonst bricht das erste ORM-Select auf news_articles/
board_memory.

Typen exakt gegen eine Bestands-DB verifiziert (information_schema):
double precision / timestamptz / uuid / text / varchar.

Bewusst NICHT enthalten: das Autogenerate-Rauschen (Index-/Constraint-/
server_default-Diffs) — kein Verhalten, nur Metadaten-Kosmetik.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

_ADDS = [
    ("news_articles", "pipeline_id", "uuid"),
    ("news_articles", "ai_score", "double precision"),
    ("news_articles", "ai_tweet", "text"),
    ("news_articles", "ai_linkedin", "text"),
    ("news_articles", "ai_category", "character varying"),
    ("news_articles", "processed_at", "timestamp with time zone"),
    ("board_memory", "frozen_at", "timestamp with time zone"),
]


def upgrade() -> None:
    for table, column, pgtype in _ADDS:
        op.execute(
            f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{column}" {pgtype}'
        )


def downgrade() -> None:
    # Nur auf frischen DBs sinnvoll rückrollbar; auf Bestands-DBs würden
    # hier historische Daten fallen — bewusst konservativ: no-op.
    pass
