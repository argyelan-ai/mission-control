"""add board_memory.content_hash + merge_candidate_id + attachments

Revision ID: 0091
Revises: 0090
Create Date: 2026-04-27

Phase 5 Memory System Hardening (MSY-02 + MSY-03). Three additive nullable
columns on board_memory:
- content_hash:        SHA-256 of normalized title+content for dedup (D-05).
- merge_candidate_id:  FK to board_memory.id, ON DELETE SET NULL (D-08).
- attachments:         JSON array of {path, mime_type, size_bytes, original_name} (D-11).

Backfill content_hash for existing rows via Python loop in the same
revision (D-09; pgcrypto-extension-free per RESEARCH.md Migration Strategy
caveat). Behaviour-preserving: existing memory entries continue to work
without any of the new fields populated (all nullable).

NO DB-level default on the new columns — null literally means "not set"
(Phase 3 Plan 03-03 lesson — null distinguishable from explicit value).
"""
from alembic import op
import sqlalchemy as sa
import hashlib


# revision identifiers, used by Alembic.
revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. content_hash: TEXT, nullable, indexed for fast hash-dedup lookup
    op.add_column("board_memory", sa.Column("content_hash", sa.Text(), nullable=True))
    op.create_index(
        "ix_board_memory_content_hash", "board_memory", ["content_hash"]
    )

    # 2. merge_candidate_id: UUID FK self-reference → board_memory.id, indexed
    op.add_column(
        "board_memory", sa.Column("merge_candidate_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_board_memory_merge_candidate",
        "board_memory", "board_memory",
        ["merge_candidate_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_board_memory_merge_candidate_id",
        "board_memory", ["merge_candidate_id"],
    )

    # 3. attachments: JSON, nullable
    op.add_column("board_memory", sa.Column("attachments", sa.JSON(), nullable=True))

    # 4. Backfill content_hash for existing rows (D-09).
    # Python-loop variant — pgcrypto-extension-free (RESEARCH.md Migration Strategy).
    # Single-user instance, < 10k rows expected. Same normalization formula as
    # the runtime helper plan 05-05 will land in routers/memory.py (lower() +
    # whitespace-collapse over "{title}\n{content}" + sha256 hex).
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, title, content FROM board_memory WHERE content_hash IS NULL")
    ).fetchall()
    for row in rows:
        norm = " ".join(f"{row.title or ''}\n{row.content or ''}".lower().split())
        h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        conn.execute(
            sa.text("UPDATE board_memory SET content_hash = :h WHERE id = :i"),
            {"h": h, "i": row.id},
        )


def downgrade() -> None:
    # Symmetric reverse: drop index → drop FK → drop column.
    op.drop_index("ix_board_memory_merge_candidate_id", table_name="board_memory")
    op.drop_constraint(
        "fk_board_memory_merge_candidate", "board_memory", type_="foreignkey"
    )
    op.drop_column("board_memory", "attachments")
    op.drop_column("board_memory", "merge_candidate_id")
    op.drop_index("ix_board_memory_content_hash", table_name="board_memory")
    op.drop_column("board_memory", "content_hash")
