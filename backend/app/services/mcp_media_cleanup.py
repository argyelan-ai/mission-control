"""Cleanup for per-task Playwright-MCP media (E2E videos, screenshots).

playwright-mcp writes into ~/.mc/mcp-screenshots/<task_id>/ (mounted as
/shared-mcp in the backend). Nothing referenced those files after a task
was deleted — the directory grew forever (review finding 05.07.).
Called best-effort from both task-delete paths; a cleanup error must
never block the entity delete.
"""

import logging
import os
import shutil
import uuid

from app.services.fs_roots import mc_home

logger = logging.getLogger("mc.mcp_media_cleanup")


def _candidate_roots() -> list[str]:
    roots = ["/shared-mcp", str(mc_home() / "mcp-screenshots")]
    return [os.path.realpath(r) for r in roots if os.path.isdir(r)]


def delete_mcp_media_for_task(task_id: uuid.UUID | str) -> int:
    """Remove <root>/<task_id>/ under every known media root. Returns count."""
    removed = 0
    name = str(task_id)
    for root in _candidate_roots():
        target = os.path.realpath(os.path.join(root, name))
        # Containment guard — task ids are UUIDs, but never trust a join.
        if not target.startswith(root + os.sep) or not os.path.isdir(target):
            continue
        try:
            shutil.rmtree(target)
            removed += 1
        except OSError as e:
            logger.warning("MCP-Media nicht löschbar (%s): %s", target, e)
    return removed
