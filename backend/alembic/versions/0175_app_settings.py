"""app_settings — global runtime settings store (channels settings page).

Operator-facing per-function channel toggles and channel targets, overriding
env defaults at runtime (services/channel_config.py). Values are strings;
types live in the service's allowlist.

Revision ID: 0175_app_settings
Revises: 0174_task_origin_thread
"""
import sqlalchemy as sa

from alembic import op

revision = "0175_app_settings"
down_revision = "0174_task_origin_thread"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index("ix_app_settings_key", "app_settings", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_app_settings_key", table_name="app_settings")
    op.drop_table("app_settings")
