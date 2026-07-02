"""runtimes power-managed columns — unsloth_porsche (PORSCHE + Wake-on-LAN).

Revision ID: 0130
Revises: 0129
Create Date: 2026-06-24

Adds three nullable / default-off columns so a runtime can describe a host that
is NOT always on (the PORSCHE Windows box with a local unsloth OpenAI server):

- control_url       — Flask control plane (e.g. http://192.0.2.20:5555), used
                      for PowerShell start/stop instead of the DGX SSH/tmux path.
- wol_mac_address   — target MAC for the Wake-on-LAN magic packet.
- power_managed     — when true, the runtime sleeps when idle: it gets a Wake
                      button in the UI and the runtime-readiness dispatch gate
                      holds tasks until it is `ready`.

All existing runtimes (DGX vLLM/LMStudio/unsloth, cloud, hermes) keep NULL /
false → unchanged behaviour. The `unsloth-porsche` row itself is seeded from
backend/config/runtimes.json by runtime_seeder (idempotent, enabled=false until
the real PORSCHE values are filled in), not here.
"""
import sqlalchemy as sa
from alembic import op


revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtimes", sa.Column("control_url", sa.String(length=512), nullable=True))
    op.add_column("runtimes", sa.Column("wol_mac_address", sa.String(length=32), nullable=True))
    op.add_column(
        "runtimes",
        sa.Column(
            "power_managed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("runtimes", "power_managed")
    op.drop_column("runtimes", "wol_mac_address")
    op.drop_column("runtimes", "control_url")
