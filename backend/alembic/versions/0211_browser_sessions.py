"""Browser sessions — one browser area per agent session or head run (ADR-088).

Each head run and each working phase of an agent gets its own session in the
shared agent browser: a row here plus an address `/s/<token>/` at cdp-gateway.
Ownership is set when the session is opened, never guessed from traffic.

The token is NOT stored (derived from id + server secret). `head_run_id` is a
plain string: head runs are folders on the host, not rows. Not a task column —
the task core is frozen (ADR-085 §4).

Constraints and partial unique indexes stand identically in the model
(app/models/browser_session.py) — tests build tables from the model,
production from this migration (convention from models/thread.py).

Additive only: nothing reads or writes this table except the new
/api/v1/browser-sessions router and services/browser_sessions.py.

Revision ID: 0211_browser_sessions
Revises: 0210_recipe_drop_page_cache
"""
import sqlalchemy as sa
from alembic import op

revision = "0211_browser_sessions"
down_revision = "0210_recipe_drop_page_cache"
branch_labels = None
depends_on = None

_NOT_ENDED = "status <> 'ended'"


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_kind", sa.String(length=16), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("head_run_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("owner_kind IN ('agent', 'head')", name="ck_browser_sessions_owner_kind"),
        sa.CheckConstraint("status IN ('open', 'live', 'ended')", name="ck_browser_sessions_status"),
    )
    op.create_index("ix_browser_sessions_agent_id", "browser_sessions", ["agent_id"])
    op.create_index("ix_browser_sessions_head_run_id", "browser_sessions", ["head_run_id"])
    op.create_index("ix_browser_sessions_status", "browser_sessions", ["status"])
    op.create_index(
        "uq_browser_sessions_open_head_run",
        "browser_sessions",
        ["head_run_id"],
        unique=True,
        postgresql_where=sa.text(f"head_run_id IS NOT NULL AND {_NOT_ENDED}"),
        sqlite_where=sa.text(f"head_run_id IS NOT NULL AND {_NOT_ENDED}"),
    )
    op.create_index(
        "uq_browser_sessions_open_agent",
        "browser_sessions",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text(f"agent_id IS NOT NULL AND {_NOT_ENDED}"),
        sqlite_where=sa.text(f"agent_id IS NOT NULL AND {_NOT_ENDED}"),
    )


def downgrade() -> None:
    op.drop_index("uq_browser_sessions_open_agent", table_name="browser_sessions")
    op.drop_index("uq_browser_sessions_open_head_run", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_status", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_head_run_id", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_agent_id", table_name="browser_sessions")
    op.drop_table("browser_sessions")
