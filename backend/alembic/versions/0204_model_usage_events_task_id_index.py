"""0204 — index on model_usage_events.task_id.

The run-record endpoint (routers/run_record.py) filters model_usage_events
by task_id — the model already declares ``Field(index=True)`` (fresh
create_all installs get the index), but migration 0127 never created it,
so migrated production DBs (~300k rows) do a full scan per run-record
fetch. Cheap, additive index; no data change. Same shape as 0156
(activity_events.task_id).

Revision ID: 0204_model_usage_task_id
Revises: 0203_claude5_model_prices
"""
from alembic import op
revision = "0204_model_usage_task_id"
down_revision = "0203_claude5_model_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_model_usage_events_task_id",
        "model_usage_events",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_usage_events_task_id", table_name="model_usage_events")
