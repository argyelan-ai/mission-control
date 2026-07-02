"""Pure helper functions for the board_memory → vault markdown cutover.

Extracted from the Alembic migration script so the logic can be unit-tested
without spinning up Postgres + Alembic. The migration script (Alembic
revision ``0112_vault_cutover.py``) imports these helpers; tests in
``backend/tests/test_vault_migration.py`` exercise them against
in-memory data.

No side effects beyond what callers pass in. ``_vault_root()`` honours the
``HOME_HOST`` env var so the same code path works inside Docker (where the
host home is bind-mounted) and on the bare host.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import frontmatter


# Memory types known to MC. The cutover only writes recognized types into
# the vault; unknown types are routed to ``global/{type}s/`` like everything
# else but are flagged in stats.
KNOWN_MEMORY_TYPES: frozenset[str] = frozenset(
    {
        "lesson",
        "knowledge",
        "reference",
        "journal",
        "weekly_review",
        "note",  # introduced by M.2 — supported defensively
    }
)


def _vault_root() -> Path:
    """Resolve the vault root directory.

    Honours ``HOME_HOST`` so Docker containers (which mount the host
    home at a different path) and the bare host share one truth.
    """
    return Path(os.environ.get("HOME_HOST", str(Path.home()))) / ".mc" / "vault"


def _slugify_agent_name(name: str | None) -> str | None:
    """Convert an agent's display name to a filesystem-safe slug.

    Mirrors :func:`app.utils.slugify` but is duplicated here so the
    migration helpers have zero runtime imports beyond the standard
    library + ``frontmatter``. Returns ``None`` if ``name`` is falsy.
    """
    if not name:
        return None
    text = name.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s_]+", "-", text)
    text = text.strip("-")
    return text or None


def _resolve_target(
    agent_slug: str | None,
    board_slug: str | None,
    mem_type: str,
    mem_id: str,
) -> Path:
    """Compute the vault-relative path for a board_memory row.

    Rules (matches plan §M.2 Task 5):

    * agent + no board → ``agents/{agent_slug}/{type}s/{id}.md``
    * board (with or without agent) → ``projects/{board_slug}/{type}s/{id}.md``
    * neither → ``global/{type}s/{id}.md``

    Memory type is pluralized by appending ``s``. Unknown types follow the
    same rule (e.g. ``weekly_review`` → ``weekly_reviews``).
    """
    type_plural = f"{mem_type}s"
    if board_slug:
        return Path("projects") / board_slug / type_plural / f"{mem_id}.md"
    if agent_slug:
        return Path("agents") / agent_slug / type_plural / f"{mem_id}.md"
    return Path("global") / type_plural / f"{mem_id}.md"


def _render_md(row: Any, agent_slug: str | None, board_slug: str | None) -> str:
    """Render a board_memory row as Markdown with frontmatter.

    ``row`` must expose attributes ``id``, ``content``, ``memory_type``,
    ``tags`` and ``created_at`` (any of these may be ``None``).

    Frontmatter shape matches the vault's required fields (see
    :mod:`app.helpers.vault_frontmatter`): ``id``, ``type``, ``agent``,
    ``date``. Adds ``tags``, ``source="migration"``, ``status="active"``
    and ``project`` (when board-scoped).
    """
    created_at = getattr(row, "created_at", None)
    if created_at is None:
        date_str = "2026-01-01T00:00:00+00:00"
    elif hasattr(created_at, "isoformat"):
        date_str = created_at.isoformat()
    else:
        date_str = str(created_at)

    tags = getattr(row, "tags", None) or []
    # Defensive normalization — DB JSON arrays sometimes round-trip as
    # tuples or odd types depending on the driver.
    if not isinstance(tags, list):
        try:
            tags = list(tags)
        except TypeError:
            tags = []

    fm: dict[str, Any] = {
        "id": str(row.id),
        "type": row.memory_type,
        "agent": agent_slug or "system",
        "date": date_str,
        "tags": tags,
        "source": "migration",
        "status": "active",
    }
    if board_slug:
        fm["project"] = board_slug

    body = row.content or ""
    post = frontmatter.Post(body, **fm)
    return frontmatter.dumps(post)


def _content_sha256(content: str) -> str:
    """Stable hash of the rendered markdown — used for idempotency check."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "KNOWN_MEMORY_TYPES",
    "_vault_root",
    "_slugify_agent_name",
    "_resolve_target",
    "_render_md",
    "_content_sha256",
]
