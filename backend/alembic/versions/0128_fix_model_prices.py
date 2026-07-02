"""Korrigiert die Modell-Preise (Seed 0127 war an Legacy-Opus orientiert).

Befunde (verifiziert an der offiziellen Anthropic-Pricing-Doku, 2026-06-12):
- claude-opus-4-* : Seed hatte 15/75 (= Legacy Opus 4.0/4.1). Aktuelle Opus 4.5-4.8
  kosten 5/25. Cache analog 0.5 / 6.25. -> 3x zu teuer berechnet.
- claude-fable-*  : Seed war Platzhalter mit Sonnet-Preis (3/15). Fable 5 kostet
  real 10/50, Cache 1.0 / 12.50.
- claude-haiku-4-5-* : fehlte komplett -> fiel auf Fallback "*" (0) -> wurde gar
  nicht berechnet. Real 1/5, Cache 0.10 / 1.25.
- claude-sonnet-4-* (3/15/0.3/3.75) war bereits korrekt -> unveraendert.
- Lokale Modelle (glm/qwen/spark/minimax) bleiben 0 (Flatrate/lokal).

Cache-Multiplikatoren = Anthropic-Standard: read 0.1x Input, write(5min) 1.25x Input.

Nach dieser Migration muessen die Event-Kosten neu berechnet werden
(POST /api/v1/model-prices/recompute bzw. Recompute-Skript).

Revision ID: 0128
Revises: 0127
Create Date: 2026-06-12
"""
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None

_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def upgrade() -> None:
    # ── Opus 4.x: 15/75 -> 5/25 (aktueller Opus 4.5-4.8 Listenpreis) ──────────
    op.execute(
        "UPDATE model_prices "
        "SET input_per_mtok=5.0, output_per_mtok=25.0, "
        "cache_read_per_mtok=0.5, cache_write_per_mtok=6.25, "
        "note='Anthropic Opus 4.5-4.8 — API Listenpreis 5/25 (verifiziert 2026-06-12). "
        "1M-Kontext ohne Aufpreis. Boss nutzt Subscription = Schattenkosten.' "
        "WHERE model_pattern='claude-opus-4-*'"
    )

    # ── Fable: Sonnet-Platzhalter -> echter Fable-5-Preis 10/50 ───────────────
    op.execute(
        "UPDATE model_prices "
        "SET input_per_mtok=10.0, output_per_mtok=50.0, "
        "cache_read_per_mtok=1.0, cache_write_per_mtok=12.5, "
        "note='Anthropic Fable 5 — API Listenpreis 10/50 (verifiziert 2026-06-12). "
        "1M-Kontext ohne Aufpreis.' "
        "WHERE model_pattern='claude-fable-*'"
    )

    # ── Haiku 4.5: fehlte komplett (wurde als 0 verrechnet) -> 1/5 ────────────
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
                "model_pattern": "claude-haiku-4-5-*",
                "input_per_mtok": 1.0,
                "output_per_mtok": 5.0,
                "cache_read_per_mtok": 0.1,
                "cache_write_per_mtok": 1.25,
                "currency": "USD",
                "valid_from": _EPOCH,
                "priority": 85,  # spezifischer als claude-*-4-* Familien-Patterns
                "note": "Anthropic Haiku 4.5 — API Listenpreis 1/5 (verifiziert 2026-06-12).",
            }
        ],
    )


def downgrade() -> None:
    # Opus zurueck auf alten Seed-Wert
    op.execute(
        "UPDATE model_prices "
        "SET input_per_mtok=15.0, output_per_mtok=75.0, "
        "cache_read_per_mtok=1.5, cache_write_per_mtok=18.75, "
        "note='Anthropic Opus 4 — API Listenpreis (Boss nutzt Subscription = Schattenkosten)' "
        "WHERE model_pattern='claude-opus-4-*'"
    )
    # Fable zurueck auf Platzhalter
    op.execute(
        "UPDATE model_prices "
        "SET input_per_mtok=3.0, output_per_mtok=15.0, "
        "cache_read_per_mtok=0.3, cache_write_per_mtok=3.75, "
        "note='Anthropic Fable — Platzhalter, bitte Preis nachpflegen' "
        "WHERE model_pattern='claude-fable-*'"
    )
    # Haiku-Row entfernen
    op.execute("DELETE FROM model_prices WHERE model_pattern='claude-haiku-4-5-*'")
