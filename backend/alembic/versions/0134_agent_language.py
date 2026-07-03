"""Agent response language — templates are English, responses are per-agent.

Revision ID: 0134
Revises: 0133
Create Date: 2026-07-03

All agent templates (SOUL.md.j2, builtin templates, protocol blocks) are
maintained in English so the public project is usable beyond German
speakers. The language an agent uses towards its operator is an
instance-level setting instead: ``agents.language`` (IETF-style short
code, default ``en``). Template rendering injects it as ``{{ language }}``
and adds an explicit response-language instruction when it differs from
English. Existing installs keep their materialized soul_md untouched;
operators who want non-English replies set the field per agent.
"""
import sqlalchemy as sa
from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("language", sa.String(length=16), nullable=False, server_default="en"),
    )


def downgrade() -> None:
    op.drop_column("agents", "language")
