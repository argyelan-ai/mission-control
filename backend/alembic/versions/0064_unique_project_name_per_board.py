"""unique project name per board — verhindert doppelte Projektnamen

Revision ID: 0064
Revises: 0063
Create Date: 2026-04-05
"""
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_projects_board_name",
        "projects",
        ["board_id", "name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_projects_board_name", "projects", type_="unique")
