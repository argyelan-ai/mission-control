"""Drop agents.language — contract step (Task 993840da, 2026-09-16).

Companion to 0201, which added `operator_language` and `work_language` but
left the old `language` column in place because the backend keeps serving
while a migration runs, and the not-yet-restarted old code still reads
`agents.language` — dropping it there would break every query against
`agents` between the DROP and the backend bounce.

This is deliberately its own deploy, cut only once the new code (reading
`operator_language`/`work_language`, never `language`) has been running
in production for a while. Once `language` is gone it only comes back
from a backup, so this waits until a grep AND observed runtime behavior
both agree nothing still touches it — an old container, a cached module,
or a code path nobody grepped could otherwise still ask for it.

Revision ID: 0202_drop_agent_language
Revises: 0201_agent_op_work_language
"""
import sqlalchemy as sa
from alembic import op

revision = "0202_drop_agent_language"
down_revision = "0201_agent_op_work_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("agents", "language")


def downgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("language", sa.String(length=16), nullable=False, server_default="en"),
    )
    op.execute("UPDATE agents SET language = operator_language")
