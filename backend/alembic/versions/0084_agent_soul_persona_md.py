"""add agents.soul_persona_md column

Revision ID: 0084
Revises: 0083
Create Date: 2026-04-20

Workstream D (see docs/superpowers/plans/2026-04-20-harness-personas-
session-handoff.md):

Adds a nullable TEXT column for each agent's persona_section — the ~80-
120 token English character voice that SOUL.md.j2 renders at the top of
every agent's SOUL. NULL means "use the generic fallback" (legacy
agents); Migration 0085 seeds the nine drafted personas afterwards.

Additive only. Idempotent via `add_column` guard-less because Alembic
tracks the version bookkeeping.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("soul_persona_md", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "soul_persona_md")
