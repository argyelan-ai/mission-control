"""0146 — runtimes.autostart_supported/autostart_flag_path (Engine Control v0, ADR-057).

First building block of Cockpit v2: a per-runtime autostart toggle that flips
a flag file on the runtime's bound host (host_id → hosts, ADR-048) via SSH. A
systemd unit on the box checks the flag on boot to decide whether to start the
inference engine. Real host/path values are set by the operator at runtime
(PATCH /runtimes/db/{slug} or the /runtimes UI) — never seeded here.

Revision ID: 0146
Revises: 0145
"""
import sqlalchemy as sa
from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtimes",
        sa.Column(
            "autostart_supported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "runtimes",
        sa.Column("autostart_flag_path", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runtimes", "autostart_flag_path")
    op.drop_column("runtimes", "autostart_supported")
