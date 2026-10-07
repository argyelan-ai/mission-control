"""Browser sessions — lifecycle columns (ADR-088 lifecycle step).

When a session first had a tab, its last browser activity, and its last image
(the file lives at <browser_sessions_root>/<id>/last.jpg; only its time and the
page it showed are stored here). All nullable, additive.

Revision ID: 0212_browser_session_lifecycle
Revises: 0211_browser_sessions
"""
import sqlalchemy as sa
from alembic import op

revision = "0212_browser_session_lifecycle"
down_revision = "0211_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("browser_sessions", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("browser_sessions", sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("browser_sessions", sa.Column("last_frame_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("browser_sessions", sa.Column("last_url", sa.String(length=2048), nullable=True))
    op.add_column("browser_sessions", sa.Column("last_title", sa.String(length=512), nullable=True))


def downgrade() -> None:
    for column in ("last_title", "last_url", "last_frame_at", "last_active_at", "started_at"):
        op.drop_column("browser_sessions", column)
