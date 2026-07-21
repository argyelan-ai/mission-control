"""bench_challenges.record_duration_s — per-challenge video length (Bench #18)

NULL = legacy RECORD_DURATION_S fallback (10s, orchestrator.py). Set by
create_challenge from the NewChallengeDialog's "Video-Länge (s)" field
(validated 5..60 at the router, mirrored by /record's own bound on the
mc-playwright sidecar).

Revision ID: 0161
Revises: 0160
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0161"
down_revision = "0160"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bench_challenges",
        sa.Column("record_duration_s", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bench_challenges", "record_duration_s")
