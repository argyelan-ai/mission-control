"""Bestehende Agents bekommen role basierend auf is_board_lead und Name-Pattern.
Composite Index auf (board_id, role).

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


# Name-Pattern → Rolle Mapping
_NAME_ROLE_MAP = {
    "planner": "planner",
    "researcher": "researcher",
    "deployer": "deployer",
    "writer": "writer",
}

_REVIEWER_PATTERNS = ("rex", "review")
_DEVELOPER_PATTERNS = ("cody", "dev", "developer")


def upgrade() -> None:
    # Composite Index fuer schnelle Board+Role Queries
    op.create_index("ix_agents_board_id_role", "agents", ["board_id", "role"])

    # Bestehende Agents: role setzen basierend auf is_board_lead + Name
    conn = op.get_bind()

    # Gueltige Enum-Werte — Agents die schon einen davon haben, ueberspringen
    _VALID_ROLES = {"lead", "developer", "reviewer", "planner", "researcher", "deployer", "writer"}

    # Board Leads → lead (auch wenn role schon gesetzt aber kein gueltiger Enum-Wert)
    # expanding-bindparam statt Tuple-Substitution: unter asyncpg (heutiges
    # alembic-env) expandiert sa.text ein Tuple nicht — Fresh-Installs
    # crashten hier (CI fresh-boot E2E, 2026-07-02). Semantik unveraendert.
    conn.execute(
        sa.text(
            "UPDATE agents SET role = 'lead' "
            "WHERE is_board_lead = true AND (role IS NULL OR role NOT IN :valid)"
        ).bindparams(sa.bindparam("valid", expanding=True)),
        {"valid": list(_VALID_ROLES)},
    )

    # Name-basiertes Mapping (fuer alle ohne gueltigen Enum-Wert)
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

        # Exakte Pattern
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
    # role-Werte nicht zuruecksetzen (kein Datenverlust)
