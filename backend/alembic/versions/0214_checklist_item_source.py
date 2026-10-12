"""Checklist rows remember where they came from (todo -> checklist sync).

The chat's todo snapshot (the model's own TodoWrite / ACP plan list) now
mirrors into the task's checklist (services/checklist_todo_sync.py). The
sync must never touch a row a person or an explicit `mc checklist` call
wrote, so every row carries its origin:

  source      "manual" (default, every existing row) | "agent_todo"
  source_key  stable identity of a synced row inside its task (normalised
              item text, "#n" for a repeated text); NULL for manual rows.

Unique (task_id, source_key) keeps the sync idempotent; manual rows have
NULL keys and never collide. Additive only: existing rows become "manual",
nothing else changes.

Revision ID: 0214_checklist_item_source
Revises: 0213_runtime_token_ledger
"""
import sqlalchemy as sa
from alembic import op

revision = "0214_checklist_item_source"
down_revision = "0213_runtime_token_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_checklist_items",
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
    )
    op.add_column(
        "task_checklist_items",
        sa.Column("source_key", sa.String(length=320), nullable=True),
    )
    op.create_unique_constraint(
        "uq_checklist_task_source_key", "task_checklist_items", ["task_id", "source_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_checklist_task_source_key", "task_checklist_items", type_="unique")
    op.drop_column("task_checklist_items", "source_key")
    op.drop_column("task_checklist_items", "source")
