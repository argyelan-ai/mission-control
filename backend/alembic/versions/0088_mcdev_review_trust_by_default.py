"""mc-dev board: review trust-by-default (ADR-023)

Revision ID: 0088
Revises: 0087
Create Date: 2026-04-20

Nach ADR-023 entscheidet der Developer/Tester/Deployer selbst, ob ein
Task ueber Review laeuft. `require_review_before_done` wird auf `mc-dev`
von `True` auf `False` gestellt. Reflektion-Pflicht bleibt davon
unberuehrt (Config-Flag `enforce_reflection`, Default True).

Idempotent. Downgrade kehrt die Policy zurueck.
"""
from alembic import op


revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trust-by-default auf dem MC-Dev-Board: Review ist opt-in.
    op.execute(
        "UPDATE boards "
        "SET require_review_before_done = FALSE "
        "WHERE slug = 'mc-dev'"
    )


def downgrade() -> None:
    # Rueckwaerts: harter Review-Gate wieder aktiv.
    op.execute(
        "UPDATE boards "
        "SET require_review_before_done = TRUE "
        "WHERE slug = 'mc-dev'"
    )
