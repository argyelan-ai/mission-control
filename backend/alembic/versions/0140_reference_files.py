"""Referenz-Dateien für Tasks & Projekte (ADR-053).

Revision ID: 0140
Revises: 0139
"""
import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_files",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("board_id", sa.Uuid(), sa.ForeignKey("boards.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("rel_path", sa.String(), nullable=False),
        sa.Column("original_name", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("uploaded_by", sa.String(), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_reference_files_board_id", "reference_files", ["board_id"])
    op.create_index("ix_reference_files_task_id", "reference_files", ["task_id"])
    op.create_index("ix_reference_files_project_id", "reference_files", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_reference_files_project_id", table_name="reference_files")
    op.drop_index("ix_reference_files_task_id", table_name="reference_files")
    op.drop_index("ix_reference_files_board_id", table_name="reference_files")
    op.drop_table("reference_files")
