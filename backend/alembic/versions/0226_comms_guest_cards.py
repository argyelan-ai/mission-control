"""Guest cards and terminal-session principals (ADR-100 stage 2c).

- comms_guest_cards: issued by the operator; the token only enrolls a
  session; stored as a SHA-256 hash; expiry; revocable.
- comms_sessions: one row per enrolled terminal session — its own principal
  (address harness@machine:folder, session token hash, expiry), never a row
  in agents.

Additive only; downgrade drops both tables.

Revision ID: 0226_comms_guest_cards
Revises: 0225_session_conversations
"""
import sqlalchemy as sa
from alembic import op

revision = "0226_comms_guest_cards"
down_revision = "0225_session_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comms_guest_cards",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_table(
        "comms_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("card_id", sa.Uuid(), sa.ForeignKey("comms_guest_cards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("address", sa.String(130), nullable=False),
        sa.Column("harness", sa.String(40), nullable=False),
        sa.Column("machine", sa.String(40), nullable=False),
        sa.Column("folder", sa.String(40), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_comms_sessions_card_id", "comms_sessions", ["card_id"])
    op.create_index("ix_comms_sessions_address", "comms_sessions", ["address"])


def downgrade() -> None:
    op.drop_index("ix_comms_sessions_address", table_name="comms_sessions")
    op.drop_index("ix_comms_sessions_card_id", table_name="comms_sessions")
    op.drop_table("comms_sessions")
    op.drop_table("comms_guest_cards")
