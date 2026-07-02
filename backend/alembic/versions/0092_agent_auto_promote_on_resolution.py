"""add agents.auto_promote_on_resolution column + deployer template data step

Revision ID: 0092
Revises: 0091
Create Date: 2026-04-27

Phase 8 Deployer Resolution Auto-Promote Fix (BUG-01) — guard both auto-promote
paths (agent_comments.py:287 + task_runner.py:771) by adding a per-agent flag
that is True for ALL existing single-step worker agents (Cody, Rex, Sparky,
Researcher, Planner, Henry, Davinci, Shakespeare, FreeCode, Neo, Tester) and
False for the deployer agent(s) whose multi-step deploy -> verify -> finalize
lifecycle conflicts with the auto-promote behaviour.

NOT NULL with server_default true ensures backward compatibility -- every
existing row gets True without an explicit UPDATE statement. The deployer
data step then UPDATEs the deployer rows to False.

Additive only. Idempotent because Alembic tracks the version bookkeeping.
See ADR-024 + Plan 03-03 for the same shape (recycler_enabled) without the
NOT NULL constraint.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "auto_promote_on_resolution",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # Data migration step (D-12): set False for all agent rows whose role is
    # 'deployer' (covers both directly-typed agents AND agents instantiated
    # from a deployer-role template -- agents.role is copied at instantiation).
    op.execute(
        "UPDATE agents SET auto_promote_on_resolution = false WHERE role = 'deployer'"
    )


def downgrade() -> None:
    op.drop_column("agents", "auto_promote_on_resolution")
