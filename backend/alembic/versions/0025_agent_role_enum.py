"""Existing agents get a role based on is_board_lead and name pattern.
Composite index on (board_id, role).

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


# Name pattern → role mapping
_NAME_ROLE_MAP = {
    "planner": "planner",
    "researcher": "researcher",
    "deployer": "deployer",
    "writer": "writer",
}

_REVIEWER_PATTERNS = ("rex", "review")
_DEVELOPER_PATTERNS = ("cody", "dev", "developer")


def upgrade() -> None:
    # Composite index for fast board+role queries
    op.create_index("ix_agents_board_id_role", "agents", ["board_id", "role"])

    # Existing agents: set role based on is_board_lead + name
    conn = op.get_bind()

    # Valid enum values — agents that already have one of these are skipped
    _VALID_ROLES = {"lead", "developer", "reviewer", "planner", "researcher", "deployer", "writer"}

    # Board leads → lead (even if role is already set but not a valid enum value)
    # expanding bindparam instead of tuple substitution: under asyncpg (current
    # alembic-env) sa.text doesn't expand a tuple — fresh installs
    # crashed here (CI fresh-boot E2E, 2026-07-02). Semantics unchanged.
    conn.execute(
        sa.text(
            "UPDATE agents SET role = 'lead' "
            "WHERE is_board_lead = true AND (role IS NULL OR role NOT IN :valid)"
        ).bindparams(sa.bindparam("valid", expanding=True)),
        {"valid": list(_VALID_ROLES)},
    )

    # Name-based mapping (for all without a valid enum value)
    agents = conn.execute(
        sa.text(
            "SELECT id, name FROM agents "
            "WHERE (role IS NULL OR role NOT IN :valid) AND name IS NOT NULL"
        ).bindparams(sa.bindparam("valid", expanding=True)),
        {"valid": list(_VALID_ROLES)},
    ).fetchall()

    for agent_id, name in agents:
        name_lower = name.lower()
        role = None

        # Exact pattern
        for pattern, mapped_role in _NAME_ROLE_MAP.items():
            if pattern in name_lower:
                role = mapped_role
                break

        if not role:
            for pattern in _REVIEWER_PATTERNS:
                if pattern in name_lower:
                    role = "reviewer"
                    break

        if not role:
            for pattern in _DEVELOPER_PATTERNS:
                if pattern in name_lower:
                    role = "developer"
                    break

        if role:
            conn.execute(
                sa.text("UPDATE agents SET role = :role WHERE id = :id"),
                {"role": role, "id": agent_id},
            )


def downgrade() -> None:
    op.drop_index("ix_agents_board_id_role", table_name="agents")
    # don't reset role values (no data loss)
