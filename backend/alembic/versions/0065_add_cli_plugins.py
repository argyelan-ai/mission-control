"""Add cli_plugins to agents and agent_templates."""

from alembic import op
import sqlalchemy as sa

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("cli_plugins", sa.JSON(), nullable=True))
    op.add_column("agent_templates", sa.Column("cli_plugins", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_templates", "cli_plugins")
    op.drop_column("agents", "cli_plugins")
