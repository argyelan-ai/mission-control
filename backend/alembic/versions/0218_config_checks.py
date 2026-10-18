"""Config checks: the latest result per checked thing (Welle 2, PR 2-1).

One row per (kind, subject_id): which registry check ran on what, ok or not,
a machine-readable reason code with its parameters (the UI words the result
from these), a short secret-free English text, how long it took, a keyed hash of the tested value
(so a replaced value reads "never tested" again) and when it ran.

Additive only: no existing table changes. Downgrade drops the table; the
check results are re-created by the next "test" click.

Revision ID: 0218_config_checks
Revises: 0217_runtime_type_flask_wol
"""
import sqlalchemy as sa
from alembic import op

revision = "0218_config_checks"
down_revision = "0217_runtime_type_flask_wol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("subject_id", sa.String(length=160), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("message", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("reason_params", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("value_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "subject_id", name="uq_config_checks_kind_subject"),
    )


def downgrade() -> None:
    op.drop_table("config_checks")
