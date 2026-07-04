"""Loops L1 — ergebnisgesteuerte Task-Schleifen (ADR-051).

Zwei Tabellen: `loops` (Definition + Laufzeit-Zustand) und `loop_rounds`
(eine Zeile pro Runde, verweist auf den Runden-Parent-Task).

Revision ID: 0138
Revises: 0137
"""
import sqlalchemy as sa
from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loops",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("board_id", sa.Uuid(), sa.ForeignKey("boards.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("goal", sa.String(), nullable=False),
        sa.Column("backlog_source", sa.String(), nullable=False, server_default="markdown"),
        sa.Column("backlog_md", sa.String(), nullable=True),
        sa.Column("backlog_tag", sa.String(), nullable=True),
        sa.Column("round_brief", sa.String(), nullable=True),
        sa.Column("human_every_n_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pause_on_failed_rounds", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("escalate_on", sa.JSON(), nullable=True),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("max_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("stop_on_backlog_empty", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("rounds_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failed_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_round_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_loops_board_id", "loops", ["board_id"])
    op.create_index("ix_loops_status", "loops", ["status"])

    op.create_table(
        "loop_rounds",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("loop_id", sa.Uuid(), sa.ForeignKey("loops.id"), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("report", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_loop_rounds_loop_id", "loop_rounds", ["loop_id"])


def downgrade() -> None:
    op.drop_index("ix_loop_rounds_loop_id", table_name="loop_rounds")
    op.drop_table("loop_rounds")
    op.drop_index("ix_loops_status", table_name="loops")
    op.drop_index("ix_loops_board_id", table_name="loops")
    op.drop_table("loops")
