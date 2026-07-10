"""0150 — tasks.blocked_at (dedicated blocked-transition timestamp).

W2-B review fix (CRITICAL B-1): the blocked-parking grace window and the
watchdog's blocked-task escalation were keyed off tasks.updated_at — a
generic onupdate=NOW timestamp that ANY metadata PATCH (title, priority,
labels) resets. That re-parked the agent for another grace window and
suppressed operator escalation indefinitely — the zombie-blocked bug
reintroduced through a side door.

blocked_at is set ONLY on the →blocked transition (SQLAlchemy attribute
listener on Task.status, covers every code path) and cleared when the task
leaves blocked. Nullable — legacy blocked rows fall back to updated_at.

Revision ID: 0150
Revises: 0149
"""
import sqlalchemy as sa
from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: currently-blocked rows get their best-known blocked time
    # (updated_at) so the grace window / escalation clock doesn't restart
    # from NULL→fallback semantics mid-flight.
    op.execute(
        "UPDATE tasks SET blocked_at = updated_at WHERE status = 'blocked'"
    )


def downgrade() -> None:
    op.drop_column("tasks", "blocked_at")
