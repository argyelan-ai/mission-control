"""Split agents.language into operator_language + work_language (Task
8d039889, 2026-09-16).

One field steered two audiences at once: how an agent replies to the
operator AND what it writes into cards (task comments, reflections,
handoffs — pure agent-to-agent traffic). The whole fleet sat on `de`, so
every card comment was German even though prompts/templates/dispatch
scaffolding are English throughout. Mark's decision: agent-to-agent goes
English, operator reports stay whatever they were.

Migration, NOT a content translation: existing German cards/reflections
stay exactly as written. This only changes what NEW agent-to-agent output
is written in going forward, via `work_language`.

Data mapping for every existing agent:
  operator_language = its current `language` (nobody's operator-facing
                       language changes on cutover)
  work_language      = "en" (the cutover itself)

`language` itself is dropped — the two new columns fully replace it. Grep
across backend/app before writing this migration found exactly three
non-test call sites (dispatch_message_builder.py, template_renderer.py,
template_seeder.py) and no frontend read/write path at all (the "rendered
but no UI control" gap named in the task); all three call sites are
updated in the same change as this migration, so nothing is left reading
a column that no longer exists.

Checked for down_revision collisions before picking one: `alembic heads`
showed a single head (0200_task_pr_reference) at branch time — no backlog
to rebase onto.

Revision ID: 0201_agent_op_work_language
Revises: 0200_task_pr_reference
"""
import sqlalchemy as sa
from alembic import op

revision = "0201_agent_op_work_language"
down_revision = "0200_task_pr_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("operator_language", sa.String(length=16), nullable=False, server_default="en"),
    )
    op.add_column(
        "agents",
        sa.Column("work_language", sa.String(length=16), nullable=False, server_default="en"),
    )
    # Backfill BEFORE dropping `language` — this is the one moment both the
    # old and the new column exist together.
    op.execute("UPDATE agents SET operator_language = language")
    op.drop_column("agents", "language")


def downgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("language", sa.String(length=16), nullable=False, server_default="en"),
    )
    op.execute("UPDATE agents SET language = operator_language")
    op.drop_column("agents", "work_language")
    op.drop_column("agents", "operator_language")
