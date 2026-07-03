"""Fresh-install schema repair — model↔chain drift (additive only)

Revision ID: 0131
Revises: 0130
Create Date: 2026-07-02

The CI fresh-boot E2E (chain 0001→0130 on an empty DB) revealed that
seven model columns historically originated via app ``create_all``/manual SQL
and exist in NO migration. Existing DBs have had them for a long time
(there, everything here is a no-op via IF NOT EXISTS); fresh installs
need them, otherwise the first ORM select on news_articles/
board_memory breaks.

Types verified exactly against an existing DB (information_schema):
double precision / timestamptz / uuid / text / varchar.

Deliberately NOT included: the autogenerate noise (index/constraint/
server_default diffs) — no behavior, just metadata cosmetics.
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
    # Only meaningfully reversible on fresh DBs; on existing DBs this
    # would drop historical data — deliberately conservative: no-op.
    pass
