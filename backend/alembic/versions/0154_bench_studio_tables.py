"""0154 — Benchmark Studio tables (bench_challenges + bench_entries).

Models live in core per ADR-044 §3 (schema identical across variants; a
stripped installation simply has idle tables). Production tracking only —
publish tail = Approval + ContentPipeline (ADR-065, no second lifecycle).

bench_entries.task_id uses ondelete=SET NULL (mc-task-delete-guard):
bench history survives task deletion, delete_task() needs no cleanup block.

Revision ID: 0154
Revises: 0153
"""
import sqlalchemy as sa
from alembic import op

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bench_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column(
            "prompt_template_id",
            sa.Uuid(),
            sa.ForeignKey("prompt_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False, server_default="side_by_side"),
        sa.Column("status", sa.String(), nullable=False, server_default="generating"),
        sa.Column("series_label", sa.String(), nullable=True),
        sa.Column("series_no", sa.Integer(), nullable=True),
        sa.Column("composed_video_path", sa.String(), nullable=True),
        sa.Column(
            "content_pipeline_id",
            sa.Uuid(),
            sa.ForeignKey("content_pipelines.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_bench_challenges_status", "bench_challenges", ["status"]
    )

    op.create_table(
        "bench_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "challenge_id",
            sa.Uuid(),
            sa.ForeignKey("bench_challenges.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_label", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("spark_model", sa.String(), nullable=True),
        sa.Column(
            "agent_id",
            sa.Uuid(),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("artifact_path", sa.String(), nullable=True),
        sa.Column("video_path", sa.String(), nullable=True),
        sa.Column("screenshot_path", sa.String(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_bench_entries_challenge_id", "bench_entries", ["challenge_id"])
    op.create_index("ix_bench_entries_task_id", "bench_entries", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_bench_entries_task_id", table_name="bench_entries")
    op.drop_index("ix_bench_entries_challenge_id", table_name="bench_entries")
    op.drop_table("bench_entries")
    op.drop_index("ix_bench_challenges_status", table_name="bench_challenges")
    op.drop_table("bench_challenges")
