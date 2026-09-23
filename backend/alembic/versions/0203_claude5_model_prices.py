"""Add model prices for the Claude 5 family (Opus 5, Opus 5.5, Sonnet 5, Fable 5.1).

Problem: model_prices only knew `claude-fable-*` from the 5 generation. The
Opus 5 / Opus 5.5 / Sonnet 5 model ids (`claude-opus-5`, `claude-opus-5-5`,
`claude-sonnet-5`) matched none of the `claude-*-4-*` patterns and fell
through to the `*` fallback (priority 0, all prices 0) -> every event of those
models was recorded with cost_usd = 0.

Prices (verified against the official Anthropic pricing page
https://platform.claude.com/docs/en/about-claude/pricing, cross-checked with
https://claude.com/pricing, 2026-09-23), USD per million tokens:

  model            input  output  cache read  cache write (5m)
  Opus 5.5          4.00   20.00     0.20         5.00   (cache read = 0.05x input)
  Opus 5            5.00   25.00     0.50         6.25
  Sonnet 5          2.00   10.00     0.20         2.50   (launch price is now the
                                                          standard price; the
                                                          planned 3/15 raise on
                                                          2026-09-01 did not happen)
  Fable 5.1        10.00   50.00     0.25        12.50   (cache read = 0.025x input)

Fable 5.1 note: `claude-fable-5-1` already matched `claude-fable-*`
(10/50/1.0/12.5 = Fable 5 price). Input/output/cache-write are identical, but
the cache-read price of Fable 5.1 is 0.25, not 1.0 -> cache reads were billed
4x too expensive. A more specific row fixes that.

Pattern matching (token_harvester.match_price): fnmatch glob, highest
priority wins, then newest valid_from. The patterns below therefore rely on
priority, not on row order:
  - `claude-opus-5-5*` (90) beats `claude-opus-5*` (85) for Opus 5.5 ids.
  - `claude-opus-5*` (85) also covers dated/suffixed Opus 5 ids.
  - `claude-sonnet-5*` (85) covers `claude-sonnet-5` and suffixed ids.
  - `claude-fable-5-1*` (80) beats `claude-fable-*` (75).
All of them sit above the local-model rows (<= 60) and the `*` fallback (0).
Caveat: a future minor release (e.g. a hypothetical Sonnet 5.5) would match
the family row until it gets its own row — the /unmatched endpoint does not
flag that, so new Claude ids need a price row when they appear.

Only list prices (standard/global) — no fast mode, batch or US-residency
multipliers. The cache-write column is the 5-minute write price (the harvester
has a single cache-write counter).

After this migration the event costs need to be recomputed
(POST /api/v1/model-prices/recompute or the recompute script).

Revision ID: 0203_claude5_model_prices
Revises: 0202_task_event_actor
Create Date: 2026-09-23
"""
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0203_claude5_model_prices"
down_revision = "0202_task_event_actor"
branch_labels = None
depends_on = None

_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)

_SOURCE = "platform.claude.com/docs/en/about-claude/pricing, verified 2026-09-23"

# (pattern, input, output, cache_read, cache_write_5m, priority, note)
PRICES = [
    ("claude-opus-5-5*", 4.0, 20.0, 0.20, 5.0, 90,
     f"Anthropic Opus 5.5 - API list price 4/20, cache read 0.05x ({_SOURCE})."),
    ("claude-opus-5*", 5.0, 25.0, 0.50, 6.25, 85,
     f"Anthropic Opus 5 - API list price 5/25 ({_SOURCE})."),
    ("claude-sonnet-5*", 2.0, 10.0, 0.20, 2.5, 85,
     f"Anthropic Sonnet 5 - API list price 2/10 ({_SOURCE})."),
    ("claude-fable-5-1*", 10.0, 50.0, 0.25, 12.5, 80,
     f"Anthropic Fable 5.1 - API list price 10/50, cache read 0.025x ({_SOURCE})."),
]


def upgrade() -> None:
    mp_table = sa.table(
        "model_prices",
        sa.column("id", sa.Uuid()),
        sa.column("model_pattern", sa.String()),
        sa.column("input_per_mtok", sa.Float()),
        sa.column("output_per_mtok", sa.Float()),
        sa.column("cache_read_per_mtok", sa.Float()),
        sa.column("cache_write_per_mtok", sa.Float()),
        sa.column("currency", sa.String()),
        sa.column("valid_from", sa.DateTime(timezone=True)),
        sa.column("priority", sa.Integer()),
        sa.column("note", sa.String()),
    )
    op.bulk_insert(
        mp_table,
        [
            {
                "id": uuid.uuid4(),
                "model_pattern": pattern,
                "input_per_mtok": inp,
                "output_per_mtok": out,
                "cache_read_per_mtok": cr,
                "cache_write_per_mtok": cw,
                "currency": "USD",
                "valid_from": _EPOCH,
                "priority": prio,
                "note": note,
            }
            for pattern, inp, out, cr, cw, prio, note in PRICES
        ],
    )


def downgrade() -> None:
    patterns = ", ".join(f"'{p[0]}'" for p in PRICES)
    op.execute(f"DELETE FROM model_prices WHERE model_pattern IN ({patterns})")
