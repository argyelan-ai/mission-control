"""runtime registry seed check — assert all JSON-defined runtimes exist in DB

Revision ID: 0094
Revises: 0093
Create Date: 2026-04-29

Phase 16 (D-04): Idempotent seed assertion. Reads backend/config/runtimes.json,
compares to runtimes table, inserts any missing rows. Existing rows are left
untouched (no drops, no updates). The JSON file is NOT removed — it remains
as the canonical seed template for fresh deploys (D-02). After this migration
the DB is the alleinige Wahrheit für GET /runtimes (D-01/D-03).

Idempotent: only INSERTs missing slugs, never UPDATEs or DROPs. Re-running is
a no-op once all seeds are present.
"""
import json
import logging
from pathlib import Path

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.0094")


def _resolve_registry_path() -> Path | None:
    """Locate runtimes.json relative to the backend package layout.

    Migration file lives at backend/alembic/versions/0094_*.py
    Config lives at         backend/config/runtimes.json
    Three parents up: backend/  → /config/runtimes.json
    """
    candidate = Path(__file__).resolve().parent.parent.parent / "config" / "runtimes.json"
    return candidate if candidate.exists() else None


def upgrade() -> None:
    registry_path = _resolve_registry_path()
    if registry_path is None:
        # JSON not in image (already removed or relocated) — DB stays as-is.
        # Bootstrap-Seed läuft via main.py lifespan über andere Pfade.
        logger.info("0094: runtimes.json not found — skipping seed check")
        return

    try:
        with open(registry_path) as f:
            seeds = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("0094: cannot parse %s: %s — skipping", registry_path, e)
        return

    conn = op.get_bind()
    existing_rows = conn.execute(sa.text("SELECT slug FROM runtimes")).fetchall()
    existing = {row[0] for row in existing_rows}

    inserted = 0
    for rt in seeds:
        # JSON uses "id" historically; DB column is "slug". Accept either.
        slug = rt.get("slug") or rt.get("id")
        if not slug or slug in existing:
            continue
        conn.execute(
            sa.text(
                # id explizit generieren: die Spalte hat keinen Server-Default —
                # auf Bestands-DBs war dieser INSERT ein No-op (Seeder war
                # schneller), auf frischen DBs crashte er (CI fresh-boot E2E).
                "INSERT INTO runtimes (id, slug, display_name, runtime_type, endpoint, enabled) "
                "VALUES (gen_random_uuid(), :slug, :dn, :rt, :ep, :en)"
            ),
            {
                "slug": slug,
                "dn": rt.get("display_name", slug),
                "rt": rt.get("runtime_type", "openai_compatible"),
                "ep": rt.get("endpoint", ""),
                "en": rt.get("enabled", True),
            },
        )
        inserted += 1

    if inserted:
        logger.info("0094: seeded %s missing runtime row(s)", inserted)


def downgrade() -> None:
    # No destructive downgrade. Seeded rows are safe to keep — they match
    # the JSON template and removing them would break the runtime registry.
    pass
