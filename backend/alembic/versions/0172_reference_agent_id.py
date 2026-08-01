"""Agent-bound reference files (Slack file ingest, ADR-053 extension).

A file the operator drops top-level into the team chat belongs to an AGENT
(usually Boss), not to a task or project — there is no task yet, the file IS
often the reason one gets created. So `reference_files` learns a third owner:

    reference_files.agent_id  (nullable, FK agents.id ON DELETE SET NULL)

SET NULL, not RESTRICT/CASCADE: a leftover reference must never block
deleting an agent (delete_agent already has enough FK gaps), and deleting
the row would silently drop the operator's file with it.

`board_id` becomes nullable in the same step: an agent may belong to no
board (`agents.board_id` is nullable), so an agent-bound reference cannot
promise one. Existing rows all carry a board and keep it.

Revision ID: 0172_reference_agent_id
Revises: 0171_operator_report_rename
Create Date: 2026-08-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0172_reference_agent_id"
down_revision = "0171_operator_report_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reference_files",
        sa.Column("agent_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_reference_files_agent_id",
        "reference_files",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_reference_files_agent_id", "reference_files", ["agent_id"]
    )
    op.alter_column("reference_files", "board_id", nullable=True)


def downgrade() -> None:
    # Agent-bound rows would violate NOT NULL on board_id — remove them first
    # (their files on disk stay untouched; only the DB rows go).
    op.execute("DELETE FROM reference_files WHERE agent_id IS NOT NULL")
    op.alter_column("reference_files", "board_id", nullable=False)
    op.drop_index("ix_reference_files_agent_id", table_name="reference_files")
    op.drop_constraint(
        "fk_reference_files_agent_id", "reference_files", type_="foreignkey"
    )
    op.drop_column("reference_files", "agent_id")
