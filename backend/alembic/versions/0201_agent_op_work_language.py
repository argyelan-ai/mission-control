"""Split agents.language into operator_language + work_language — expand
step only (Task 8d039889 / 993840da, 2026-09-16).

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

Expand/contract split (993840da): this revision was originally written to
also `DROP COLUMN language` in the same transaction. Rex verified that on
a scratch DB, where it is correct — but the backend keeps serving reads
and writes against `agents` while the migration runs, and the
not-yet-restarted old code still reads `agents.language`. Between the
DROP and the backend bounce, every query against `agents` fails, and
fourteen agents hang off that table. So `language` stays for now: this
revision is additive only (add the two columns, backfill
`operator_language`, leave `language` untouched — the application code
must not write to it). The drop moves to a later contract-only migration,
its own deploy, once the new code has been running.

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
    # `language` stays present after this migration (see module docstring) —
    # the drop moves to a later contract-only migration, a separate deploy.
    op.execute("UPDATE agents SET operator_language = language")


def downgrade() -> None:
    op.drop_column("agents", "work_language")
    op.drop_column("agents", "operator_language")
