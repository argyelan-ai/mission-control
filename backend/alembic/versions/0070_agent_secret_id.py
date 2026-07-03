"""add secret_id FK to agent for per-agent API-Key selection

Revision ID: 0070
Revises: 0069
Create Date: 2026-04-09

Context: feature "per-agent API key selector". Agent gets an optional
foreign key to the secrets table. During sync-config, the associated
value is decrypted and written as an .env file into the container
(docker_agent_sync.py). Nullable → existing agents stay functional,
docker-compose.yml env acts as fallback. ON DELETE SET NULL → deleting
a secret does not crash an agent.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0070'
down_revision = '0069'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'agents',
        sa.Column('secret_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_agents_secret_id',
        source_table='agents',
        referent_table='secrets',
        local_cols=['secret_id'],
        remote_cols=['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_agents_secret_id',
        'agents',
        ['secret_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_agents_secret_id', table_name='agents')
    op.drop_constraint('fk_agents_secret_id', 'agents', type_='foreignkey')
    op.drop_column('agents', 'secret_id')
