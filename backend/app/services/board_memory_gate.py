"""Write switch for the legacy ``board_memory`` table.

``board_memory`` is the second truth next to the vault (ADR-102 principle 6,
ADR-104). With ``BOARD_MEMORY_WRITE_ENABLED`` off (the default) no new rows
are written and the API no longer edits or deletes rows. Reading stays: the
old rows remain visible and searchable. Background maintenance still sets its
flags on existing rows (``vault_cleanup`` archived_at, ``vault_decay``,
``memory_indexing`` merge candidates) — that is bookkeeping, not new content.

What stops with the switch off — there is no vault writer for it yet, so
these are not moved, they are no longer recorded: auto lessons (task failed,
review feedback, lead-approved reflection), phase knowledge, the weekly
review, the daily intelligence report, and the deliverable mirror (the
deliverable's vault copy stays). One write stays open on purpose: the
operator's own group-chat "keep the result" (``routers/groups.py``), until
the group tab goes.

Two ways to ask:

- API routes add ``Depends(require_board_memory_writes)``; with the switch off
  they answer 410 and do nothing.
- Background writers call ``board_memory_write_skipped(where)`` first and
  return early when it is True (one log line, no exception).

Way back: ``BOARD_MEMORY_WRITE_ENABLED=true`` in the backend and worker
environment, then restart both. Nothing has to be restored, no row is lost.

``tests/test_board_memory_write_gate.py`` checks that every ``BoardMemory``
write site asks this gate before it writes, and every mutating legacy memory
route carries the dependency (with the one listed exemption).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app import config as _config

logger = logging.getLogger("mc.board_memory_gate")

#: Places that already logged their skip at INFO in this process. The
#: intelligence loop asks every 10 minutes; one INFO line per place is enough,
#: the rest go to DEBUG.
_logged_skips: set[str] = set()

DISABLED_DETAIL = (
    "The legacy board memory is read-only (BOARD_MEMORY_WRITE_ENABLED=false). "
    "Existing entries can still be read; write new notes to the vault instead."
)


def board_memory_writes_enabled() -> bool:
    """True when writes to ``board_memory`` are allowed (switch on)."""
    return bool(getattr(_config.settings, "board_memory_write_enabled", False))


def board_memory_write_skipped(where: str) -> bool:
    """True when the switch is off — the caller must return without writing."""
    if board_memory_writes_enabled():
        return False
    level = logging.DEBUG if where in _logged_skips else logging.INFO
    _logged_skips.add(where)
    logger.log(level, "board_memory write skipped (%s): BOARD_MEMORY_WRITE_ENABLED is off", where)
    return True


async def require_board_memory_writes() -> None:
    """FastAPI dependency: 410 Gone while the switch is off."""
    if not board_memory_writes_enabled():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=DISABLED_DETAIL)
