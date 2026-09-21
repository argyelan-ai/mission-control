"""Add actor_user_id / actor_label to task_events.

task_events.changed_by='user' carries no author today — an operator
clicking in the browser and a Claude session using the service account
look identical. This adds an optional actor to every event: the logged-in
user for operator-triggered changes, or a plain text label ("telegram")
for channels that are not a `users` row.

Revision ID: 0202_task_event_actor
Revises: 0201_agent_op_work_language
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0202_task_event_actor"
down_revision = "0201_agent_op_work_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No ondelete here — matches the SQLModel field in app/models/task.py
    # (TaskEvent.actor_user_id), which carries no ondelete either. Keeping
    # migration and model in lockstep avoids the drift a later
    # --autogenerate would otherwise "fix" on its own.
    op.add_column(
        "task_events",
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", name="fk_task_events_actor_user_id_users"),
            nullable=True,
        ),
    )
    op.add_column(
        "task_events",
        sa.Column("actor_label", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_events", "actor_label")
    op.drop_column("task_events", "actor_user_id")
