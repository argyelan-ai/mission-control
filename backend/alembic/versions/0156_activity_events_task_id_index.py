"""0156 — index on activity_events.task_id (Task Flight Recorder).

The new GET .../tasks/{task_id}/timeline endpoint (task-flight-recorder)
correlates activity_events by task_id. That column had no index (only
board_id is indexed) — on a table that already carries ~85k+ rows in
production (cost_collector revival, Jul 4), every timeline fetch would be
a full table scan. Cheap, additive index; no data change.

Revision ID: 0156
Revises: 0155
"""
import sqlalchemy as sa
from alembic import op

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_activity_events_task_id",
        "activity_events",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_activity_events_task_id", table_name="activity_events")
