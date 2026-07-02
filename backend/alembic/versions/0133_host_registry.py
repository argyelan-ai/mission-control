"""Host Registry — hosts table + runtimes.host_id (ADR-048, Welle 1).

Revision ID: 0133
Revises: 0132
Create Date: 2026-07-02

Generic multi-host control-plane: a `hosts` row describes a machine the
backend can reach (kind ssh | flask_wol | local); runtimes bind to it via
`runtimes.host_id` (SET NULL on host delete — the runtime survives and
falls back to its legacy fields / settings.dgx_ssh_host, see host_resolver).

The legacy per-runtime columns (host, control_url, wol_mac_address,
power_managed) are intentionally NOT dropped — they stay as back-compat
fallback so existing installations behave byte-identically. Seeding of
`dgx-spark` / `porsche` rows happens in the lifespan (host_seeder.py),
not here: fresh installs without a GPU box get 0 hosts and 0 errors.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ssh_host", sa.String(length=128), nullable=True),
        sa.Column("ssh_user", sa.String(length=64), nullable=True),
        sa.Column("ssh_key_path", sa.String(length=512), nullable=True),
        sa.Column("control_url", sa.String(length=512), nullable=True),
        sa.Column("wol_mac_address", sa.String(length=32), nullable=True),
        sa.Column("power_managed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("ui_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )

    op.add_column(
        "runtimes",
        sa.Column(
            "host_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hosts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_runtimes_host_id", "runtimes", ["host_id"])


def downgrade() -> None:
    op.drop_index("ix_runtimes_host_id", table_name="runtimes")
    op.drop_column("runtimes", "host_id")
    op.drop_table("hosts")
