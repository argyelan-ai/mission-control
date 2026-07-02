"""project_system_phase1 — project_phases, deliverable_references, model extensions

Revision ID: 0063
Revises: 0062
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- New table: project_phases ---
    op.create_table(
        "project_phases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("depends_on_phases", sa.JSON(), nullable=True),
        sa.Column("gate_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failure_policy", sa.String(), nullable=False, server_default="retry"),
        sa.Column(
            "default_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("git_branch", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_project_phases_project_id", "project_phases", ["project_id"])

    # --- New table: deliverable_references ---
    op.create_table(
        "deliverable_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_deliverable_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_deliverables.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_deliverable_references_source_deliverable_id",
        "deliverable_references",
        ["source_deliverable_id"],
    )
    op.create_index(
        "ix_deliverable_references_target_project_id",
        "deliverable_references",
        ["target_project_id"],
    )

    # --- New columns on projects ---
    op.add_column("projects", sa.Column("briefing_doc", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column(
            "parent_project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "projects",
        sa.Column("last_active_phase_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("projects", sa.Column("resume_briefing", sa.Text(), nullable=True))

    # --- New columns on task_deliverables ---
    op.add_column("task_deliverables", sa.Column("content", sa.Text(), nullable=True))
    op.add_column(
        "task_deliverables",
        sa.Column("scope", sa.String(), nullable=False, server_default="task"),
    )
    op.add_column("task_deliverables", sa.Column("tags", sa.JSON(), nullable=True))
    op.add_column(
        "task_deliverables",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "task_deliverables",
        sa.Column("is_reusable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "task_deliverables", sa.Column("git_commit_hash", sa.String(), nullable=True)
    )

    # --- New columns on tasks ---
    op.add_column(
        "tasks",
        sa.Column(
            "phase_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_phases.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_tasks_phase_id", "tasks", ["phase_id"])
    op.add_column(
        "tasks",
        sa.Column(
            "triggered_by_deliverable_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_deliverables.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # tasks
    op.drop_column("tasks", "triggered_by_deliverable_id")
    op.drop_index("ix_tasks_phase_id", table_name="tasks")
    op.drop_column("tasks", "phase_id")

    # task_deliverables
    op.drop_column("task_deliverables", "git_commit_hash")
    op.drop_column("task_deliverables", "is_reusable")
    op.drop_column("task_deliverables", "is_pinned")
    op.drop_column("task_deliverables", "tags")
    op.drop_column("task_deliverables", "scope")
    op.drop_column("task_deliverables", "content")

    # projects
    op.drop_column("projects", "resume_briefing")
    op.drop_column("projects", "last_active_phase_id")
    op.drop_column("projects", "parent_project_id")
    op.drop_column("projects", "briefing_doc")

    # tables
    op.drop_index("ix_deliverable_references_target_project_id", table_name="deliverable_references")
    op.drop_index("ix_deliverable_references_source_deliverable_id", table_name="deliverable_references")
    op.drop_table("deliverable_references")
    op.drop_index("ix_project_phases_project_id", table_name="project_phases")
    op.drop_table("project_phases")
