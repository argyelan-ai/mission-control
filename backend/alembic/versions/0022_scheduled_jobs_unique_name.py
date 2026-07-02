"""scheduled_jobs_unique_name

Revision ID: 0022
Revises: 0021
Create Date: 2026-03-01

Duplikate in scheduled_jobs bereinigen + UNIQUE Constraint auf name.
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Duplikate löschen — älteste Einträge behalten (kleinste created_at)
    op.execute("""
        DELETE FROM scheduled_jobs
        WHERE id NOT IN (
            SELECT DISTINCT ON (name) id
            FROM scheduled_jobs
            ORDER BY name, created_at ASC
        )
    """)
    # UNIQUE Constraint hinzufügen
    op.create_unique_constraint("uq_scheduled_jobs_name", "scheduled_jobs", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_scheduled_jobs_name", "scheduled_jobs", type_="unique")
