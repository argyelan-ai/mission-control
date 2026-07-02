"""agent_templates table + agents.template_id

Revision ID: 0012
Revises: 0011
Create Date: 2026-02-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Neue Tabelle agent_templates
    op.create_table(
        "agent_templates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("emoji", sa.String(), nullable=False, server_default="🤖"),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("default_model", sa.String(), nullable=True),
        sa.Column("soul_md", sa.Text(), nullable=True),
        sa.Column("skills", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_templates_name", "agent_templates", ["name"])

    # 2. template_id auf agents
    op.add_column("agents", sa.Column("template_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_agents_template_id",
        "agents",
        "agent_templates",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 3. Index auf gateway_agent_id (schnelleres Sync-Lookup)
    op.create_index("ix_agents_gateway_agent_id", "agents", ["gateway_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_gateway_agent_id", table_name="agents")
    op.drop_constraint("fk_agents_template_id", "agents", type_="foreignkey")
    op.drop_column("agents", "template_id")
    op.drop_index("ix_agent_templates_name", table_name="agent_templates")
    op.drop_table("agent_templates")
