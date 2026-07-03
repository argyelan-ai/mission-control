"""mc-dev board: review trust-by-default (ADR-023)

Revision ID: 0088
Revises: 0087
Create Date: 2026-04-20

Per ADR-023, the developer/tester/deployer decides for themselves whether
a task goes through review. `require_review_before_done` is set on
`mc-dev` from `True` to `False`. The reflection requirement is unaffected
by this (config flag `enforce_reflection`, default True).

Idempotent. Downgrade reverts the policy.
"""
from alembic import op


revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trust-by-default on the MC Dev board: review is opt-in.
    op.execute(
        "UPDATE boards "
        "SET require_review_before_done = FALSE "
        "WHERE slug = 'mc-dev'"
    )


def downgrade() -> None:
    # Reverting: hard review gate active again.
    op.execute(
        "UPDATE boards "
        "SET require_review_before_done = TRUE "
        "WHERE slug = 'mc-dev'"
    )
