"""Index of the knowledge root (ADR-104): knowledge_entries.

First slice of "knowledge in one place" (ADR-102, principle 6). Additive
only — no existing table is touched. The table is an index over Markdown
files (the files are the truth) and can be emptied and rebuilt at any time.
Everything that writes it is behind ``KNOWLEDGE_ROOT_ENABLED`` (default off).

Search: a GIN index over ``to_tsvector('simple', search_text)`` — 'simple'
on purpose (no stemming), because notes mix German and English.

Way back: flag off, then ``downgrade`` (drops the table) or
``TRUNCATE knowledge_entries``.

Revision ID: 0228_knowledge_root
Revises: 0227_chat_record
"""
import sqlalchemy as sa
from alembic import op

revision = "0228_knowledge_root"
down_revision = "0227_chat_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("drawer", sa.String(20), nullable=False),
        sa.Column("project_slug", sa.String(120), nullable=True),
        sa.Column("project_hint", sa.String(120), nullable=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False, server_default="note"),
        sa.Column("title", sa.String(300), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("search_text", sa.Text(), nullable=True),
        sa.Column("problem", sa.String(60), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("source", "rel_path", name="uq_knowledge_entries_path"),
    )
    op.create_index("ix_knowledge_entries_drawer_project", "knowledge_entries", ["drawer", "project_slug"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_knowledge_entries_search ON knowledge_entries "
            "USING GIN (to_tsvector('simple', coalesce(search_text, '')))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_knowledge_entries_search")
    op.drop_index("ix_knowledge_entries_drawer_project", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
