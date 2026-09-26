"""0205 — head runs in model_usage_events.

Head runs (docs/specs/head-launcher.md) get their own usage rows: the run
folder id (``head_run_id``) and the runtime's locality (``local`` | ``cloud``)
from the run's spec.json. Both nullable — every existing row is a non-head
row. Additive, no data change.

Revision ID: 0205_model_usage_head_runs
Revises: 0204_model_usage_task_id
"""
import sqlalchemy as sa
from alembic import op

revision = "0205_model_usage_head_runs"
down_revision = "0204_model_usage_task_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_usage_events", sa.Column("head_run_id", sa.String(), nullable=True))
    op.add_column("model_usage_events", sa.Column("locality", sa.String(), nullable=True))
    op.create_index("ix_model_usage_events_head_run_id", "model_usage_events", ["head_run_id"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_events_head_run_id", table_name="model_usage_events")
    op.drop_column("model_usage_events", "locality")
    op.drop_column("model_usage_events", "head_run_id")
