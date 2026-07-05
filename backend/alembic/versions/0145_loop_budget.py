"""0145 — loops.budget_usd/budget_tokens (Loops L3: Kosten-Budget).

Geprüft an Rundengrenzen gegen die Summe der task-attribuierten
model_usage_events aller Runden-Tasks des Loops.

Revision ID: 0145
Revises: 0144
"""
import sqlalchemy as sa
from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("loops", sa.Column("budget_usd", sa.Float(), nullable=True))
    op.add_column("loops", sa.Column("budget_tokens", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("loops", "budget_tokens")
    op.drop_column("loops", "budget_usd")
