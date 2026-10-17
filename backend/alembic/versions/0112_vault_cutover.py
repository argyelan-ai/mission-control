"""Vault cutover migration: board_memory rows → Markdown files in ~/.mc/vault/

Revision: 0112
Revises: 0111

This migration is the M.2 (Vault Memory Foundation) cutover. It copies every
row in ``board_memory`` into the on-disk vault as a Markdown file with a
YAML frontmatter that includes the row's original UUID as ``id`` — which is
the bridge that closes M.1's read-side schema gap.

Properties:

* **Idempotent.** Re-running produces the same result. If a vault file
  already exists with the same SHA-256 of the rendered content, the row
  is skipped (it does not overwrite — the operator may have edited the file
  manually during the soak window).
* **Non-destructive to vault.** Existing files are NEVER overwritten or
  deleted by this migration, even if the DB row differs.
* **Reversible-ish.** ``downgrade()`` removes the ``frozen_at`` column.
  Vault files are NOT deleted — they remain as a read-only history.
* **Soft-locks board_memory.** Adds ``frozen_at`` column and stamps every
  row with ``NOW()``. The application layer treats non-null ``frozen_at``
  as a 2-week soak marker; M.5 later drops the table entirely.

OPERATOR STEPS (run manually after merging this commit):

1. Backup current state::

       ./backup.sh

2. Apply migration::

       docker compose exec -u mcuser backend alembic upgrade head

3. Verify counts::

       docker compose exec db psql -U mc mission_control -c \
           "SELECT COUNT(*) FROM board_memory WHERE frozen_at IS NOT NULL;"
       find ~/.mc/vault/memory -name "*.md" | wc -l

   Both numbers should match the pre-migration row count (~800).

4. Spot-check files have the ``id`` frontmatter::

       head -12 ~/.mc/vault/memory/agents/sparky/lessons/*.md | head -30

   Each file must start with ``---`` and contain ``id: <uuid>``.

5. After the 2-week soak (M.5) the ``board_memory`` table will be dropped.

ROLLBACK::

    docker compose exec -u mcuser backend alembic downgrade -1

    This removes the ``frozen_at`` column only. Vault files remain on
    disk — they are now stale read-only history, but no data is lost.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

# Make the backend app package importable from inside the Alembic env.
# Alembic invokes this script via env.py which already adds ``backend/`` to
# sys.path, but we belt-and-brace it here because the helper module lives
# under ``app.services`` and we want the same import path to work when the
# script is exec'd via importlib.util in tooling/dry-run checks.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services.vault_migration_helpers import (  # noqa: E402
    KNOWN_MEMORY_TYPES,
    _content_sha256,
    _render_md,
    _resolve_target,
    _slugify_agent_name,
    _vault_root,
)


# revision identifiers, used by Alembic.
revision: str = "0112"
down_revision: Union[str, None] = "0111"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


logger = logging.getLogger("alembic.runtime.migration.vault_cutover")


def upgrade() -> None:
    """Phase 1: copy board_memory rows to vault markdown files.
    Phase 2: add ``frozen_at`` column and stamp every row.

    The vault copy is best-effort per-row: a single row that fails to
    serialize will be logged and counted as an error, but the rest of the
    migration continues. This is intentional — we do NOT want one bad row
    to roll back the schema change for 800 good rows.
    """
    copied = _migrate_rows_to_vault()
    _add_frozen_at_column(stamp=copied)


def downgrade() -> None:
    """Remove the ``frozen_at`` soft-lock column.

    Vault files are intentionally **not** deleted. They become stale
    read-only history if you roll back. There is no safe way to delete
    them automatically — the operator may have manually edited some — so the
    operator must clean them up by hand if they really want a clean
    slate.
    """
    op.drop_column("board_memory", "frozen_at")


# ---------------------------------------------------------------------------
# Helpers — phase implementations
# ---------------------------------------------------------------------------


def _migrate_rows_to_vault() -> bool:
    """Copy rows to the vault. Returns False when the copy was skipped.

    File-system rule (2026-10, fresh installs with a host uid != 1000):
    a migration must not create directories. On a fresh database there is
    nothing to copy, so this touches the disk not at all — the vault tree is
    created by the boot step (app.services.mc_home_preflight). When there ARE
    rows but ~/.mc is not writable, the copy is skipped with a warning instead
    of crashing the whole migration chain; the rows stay in board_memory
    unstamped (frozen_at NULL), nothing is lost.
    """
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT
                bm.id           AS id,
                bm.content      AS content,
                bm.memory_type  AS memory_type,
                bm.agent_id     AS agent_id,
                bm.board_id     AS board_id,
                bm.tags         AS tags,
                bm.created_at   AS created_at,
                a.name          AS agent_name,
                b.slug          AS board_slug
            FROM board_memory bm
            LEFT JOIN agents a ON bm.agent_id = a.id
            LEFT JOIN boards b ON bm.board_id = b.id
            """
        )
    ).fetchall()

    if not rows:
        logger.info("Vault cutover: board_memory is empty — nothing to copy, disk untouched")
        return True

    vault = _vault_root()
    memory_root = vault / "memory"
    try:
        memory_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = (
            f"Vault cutover SKIPPED: cannot create {memory_root} ({exc}). "
            f"{len(rows)} board_memory rows stay in the database unstamped. "
            "The backend user cannot write ~/.mc — set PUID/PGID in .env to the "
            "host user's `id -u`/`id -g` (docs/handbook/operations/platforms.md#host-user-id-linux-nas)."
        )
        print(msg)
        logger.warning(msg)
        return False

    stats = {
        "total": len(rows),
        "written": 0,
        "skipped_same_sha": 0,
        "skipped_existing_different": 0,
        "unknown_type": 0,
        "errors": 0,
    }

    for row in rows:
        try:
            agent_slug = _slugify_agent_name(row.agent_name)
            board_slug = row.board_slug
            mem_type = row.memory_type or "knowledge"
            if mem_type not in KNOWN_MEMORY_TYPES:
                stats["unknown_type"] += 1

            rel_path = _resolve_target(agent_slug, board_slug, mem_type, str(row.id))
            target = memory_root / rel_path

            content = _render_md(row, agent_slug, board_slug)
            new_sha = _content_sha256(content)

            if target.exists():
                old_sha = _content_sha256(target.read_text(encoding="utf-8"))
                if old_sha == new_sha:
                    stats["skipped_same_sha"] += 1
                    continue
                # File exists with different content. Do NOT overwrite —
                # the operator or an agent may have edited it. Just count.
                stats["skipped_existing_different"] += 1
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            stats["written"] += 1

        except Exception as exc:  # pragma: no cover — best-effort logging
            logger.error(
                "Vault cutover: failed to render row %s: %s",
                getattr(row, "id", "?"),
                exc,
            )
            stats["errors"] += 1

    # Print AND log so the result is visible whether Alembic is run
    # interactively or under docker compose logs.
    summary = f"Vault migration complete: {stats}"
    print(summary)
    logger.info(summary)
    return True


def _add_frozen_at_column(stamp: bool = True) -> None:
    """Add ``frozen_at`` column + stamp every existing row with NOW().

    Idempotent against re-run: if the column already exists, we still
    re-stamp NULL rows (no-op if everything was stamped previously).
    ``stamp=False`` (the vault copy was skipped) adds the column only, so
    the uncopied rows stay recognisable by ``frozen_at IS NULL``.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("board_memory")}

    if "frozen_at" not in existing_columns:
        op.add_column(
            "board_memory",
            sa.Column("frozen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )

    if not stamp:
        return
    # Stamp any rows that don't have it yet. Using NOW() in SQL keeps the
    # timestamp in the DB's clock — matches what subsequent triggers expect.
    op.execute(
        "UPDATE board_memory SET frozen_at = NOW() WHERE frozen_at IS NULL"
    )
