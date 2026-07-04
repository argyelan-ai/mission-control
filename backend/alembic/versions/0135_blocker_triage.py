"""boards.blocker_triage_minutes — Lead-first Blocker-Triage (Autonomy Hardening Fix A)

Blocker gehen zuerst an den Board-Lead (Triage-Fenster in Minuten), erst nach
Ablauf an den Operator. 0 = altes Verhalten (direkt Operator-Approval).

Revision ID: 0135
Revises: 0134
"""
from alembic import op
import sqlalchemy as sa

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column(
            "blocker_triage_minutes",
            sa.Integer(),
            nullable=False,
            server_default="15",
        ),
    )


def downgrade() -> None:
    op.drop_column("boards", "blocker_triage_minutes")
