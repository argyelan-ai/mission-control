"""review_decision — Explizite Review-Entscheidungen statt impliziter Status-Aenderungen.

Revision ID: 0036
Revises: 0035
"""

from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("review_decision", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("review_decided_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "review_decided_at")
    op.drop_column("tasks", "review_decision")
