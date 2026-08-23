"""Gruppen — Multi-Agent-Gruppenchat V1 (ADR-075).

Drei Tabellen: `agent_groups` (Config + Laufzeit-Zustand, 1:1 an einem
Thread(kind="group")), `group_members` (Teilnahme, Composite-PK) und
`group_rounds` (eine Zeile pro Runde, Zustand für Recovery der Engine).

Constraints stehen identisch im Modell (app/models/group.py) UND hier —
Tests bauen die Tabellen aus dem Modell, Produktion aus der Migration; nur
wenn beide dieselben Constraints tragen, prüft der Test das
Produktionsverhalten (Konvention aus models/thread.py).

Revision ID: 0181
Revises: 0180_host_tailscale_address
"""
import sqlalchemy as sa
from alembic import op

revision = "0181_agent_groups"
down_revision = "0180_host_tailscale_address"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_groups",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("goal", sa.String(), nullable=False),
        sa.Column("lifecycle", sa.String(), nullable=False, server_default="one_shot"),
        sa.Column(
            "lead_agent_id",
            sa.Uuid(),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("budget_usd", sa.Float(), nullable=True),
        sa.Column("budget_tokens", sa.BigInteger(), nullable=True),
        sa.Column("human_every_n_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pause_on_failed_rounds", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("operator_reports", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("speaker_timeout_seconds", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("live_max_turns_per_impulse", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("result_doc_rel_path", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("rounds_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failed_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_round_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("thread_id", name="uq_agent_groups_thread_id"),
    )
    op.create_index("ix_agent_groups_thread_id", "agent_groups", ["thread_id"])
    op.create_index("ix_agent_groups_status", "agent_groups", ["status"])

    op.create_table(
        "group_members",
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("agent_groups.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.Uuid(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("role", sa.String(), nullable=False, server_default="member"),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_table(
        "group_rounds",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("agent_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="autonomous"),
        sa.Column("brief_seq", sa.Integer(), nullable=True),
        sa.Column("pending_speakers", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("lead_prompt_seq", sa.Integer(), nullable=True),
        sa.Column(
            "trigger_message_id",
            sa.Uuid(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("report", sa.String(), nullable=True),
        sa.Column("doc_snapshot", sa.Text(), nullable=True),
        sa.Column("tokens_used", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_group_rounds_group_id", "group_rounds", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_group_rounds_group_id", table_name="group_rounds")
    op.drop_table("group_rounds")
    op.drop_table("group_members")
    op.drop_index("ix_agent_groups_status", table_name="agent_groups")
    op.drop_index("ix_agent_groups_thread_id", table_name="agent_groups")
    op.drop_table("agent_groups")
