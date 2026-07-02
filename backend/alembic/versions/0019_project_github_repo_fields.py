"""project github repo fields

Revision ID: 0019
Revises: 0018
Create Date: 2026-03-01

Neue Felder auf projects: github_repo_url, github_repo_name (beide nullable).
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("github_repo_url", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("github_repo_name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "github_repo_name")
    op.drop_column("projects", "github_repo_url")
