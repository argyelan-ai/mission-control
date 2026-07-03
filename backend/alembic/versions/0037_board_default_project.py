"""Board default_project_id — automatic project assignment for tasks.

Revision ID: 0037
Revises: 0036
"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column("default_project_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_boards_default_project_id",
        "boards",
        "projects",
        ["default_project_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_boards_default_project_id", "boards", type_="foreignkey")
    op.drop_column("boards", "default_project_id")
