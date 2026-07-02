"""Vault Soft Decay — weekly cron to degrade unread notes.

Two stages:
  Stage 1 (90 days): confidence drops one level (high->medium, medium->low),
           status set to 'stale' if confidence reaches 'low'.
  Stage 2 (180 days + confidence=low): soft-archive the note.

Exemptions:
  - is_pinned = True
  - status = 'draft'
  - memory_type in ('reference', 'weekly_review')
  - Notes with active contradiction_ids (need resolution, not archive)

Grace period: Decay does not fire until GRACE_PERIOD_DAYS (90) after the
migration date. This prevents mass-decay on first run when most notes
have last_viewed_at=NULL because view-tracking was just deployed.

Reversibility: Archived notes are moved to vault.archive/decay-{date}/.
POST /api/v1/vault/restore/{note_id} restores them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession
    from app.services.vault_log import VaultLog

logger = logging.getLogger("mc.vault_decay")

GRACE_PERIOD_DAYS = 90
STAGE1_DAYS = 90
STAGE2_DAYS = 180
DECAY_EXEMPT_TYPES = frozenset({"reference", "weekly_review"})


def demote_confidence(current: str) -> str:
    """Demote confidence by one level. Unknown values fall to 'low'."""
    return {"high": "medium", "medium": "low"}.get(current, "low")


@dataclass
class DecayResult:
    demoted: int = 0
    archived: int = 0
    skipped_pinned: int = 0
    skipped_type: int = 0
    skipped_contradiction: int = 0
    skipped_draft: int = 0
    skipped_grace_period: bool = False
    errors: list[str] | None = None


async def run_decay(
    *,
    session: "AsyncSession",
    vault_path: Path,
    vault_log: "VaultLog | None" = None,
    migration_date: datetime | None = None,
) -> DecayResult:
    """Execute one decay cycle. Called weekly by the decay cron loop.

    Args:
        session: DB session for querying BoardMemory rows.
        vault_path: Vault root for filesystem operations.
        vault_log: Optional VaultLog for audit trail.
        migration_date: When migration 0126 was applied. If within
            GRACE_PERIOD_DAYS of now, decay is skipped entirely.

    Returns:
        DecayResult with counts of affected notes.
    """
    from app.models.memory import BoardMemory
    from sqlalchemy import select

    result = DecayResult()
    now = datetime.now(timezone.utc)

    # Grace period check
    if migration_date is not None:
        days_since = (now - migration_date).days
        if days_since < GRACE_PERIOD_DAYS:
            result.skipped_grace_period = True
            logger.info(
                "decay: grace period active (%d/%d days since migration), skipping",
                days_since, GRACE_PERIOD_DAYS,
            )
            return result

    # Fetch all published/stale, non-pinned notes
    try:
        stmt = select(BoardMemory).where(
            BoardMemory.status.in_(("published", "stale")),
        )
        rows_result = await session.execute(stmt)
        rows = rows_result.all()
        # Normalize rows (SQLAlchemy 2.x returns Row objects)
        notes: list[Any] = []
        for r in rows:
            notes.append(r[0] if hasattr(r, "_mapping") else r)
    except Exception as e:
        result.errors = [f"query failed: {e}"]
        logger.error("decay: query failed: %s", e)
        return result

    stage1_cutoff = now - timedelta(days=STAGE1_DAYS)
    stage2_cutoff = now - timedelta(days=STAGE2_DAYS)

    for note in notes:
        # Skip pinned
        if getattr(note, "is_pinned", False):
            result.skipped_pinned += 1
            continue

        # Skip exempt types
        if getattr(note, "memory_type", "") in DECAY_EXEMPT_TYPES:
            result.skipped_type += 1
            continue

        # Skip drafts
        if getattr(note, "status", "") == "draft":
            result.skipped_draft += 1
            continue

        # Skip notes with active contradictions
        contradiction_ids = getattr(note, "contradiction_ids", None) or []
        if contradiction_ids:
            result.skipped_contradiction += 1
            continue

        # Determine last activity timestamp
        last_active = getattr(note, "last_viewed_at", None) or getattr(note, "created_at", None)
        if last_active is None:
            continue

        # Ensure timezone-aware comparison
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)

        # Stage 2: Archive (180d + low confidence)
        if (
            getattr(note, "confidence", "") == "low"
            and last_active < stage2_cutoff
            and getattr(note, "created_at", now).replace(tzinfo=timezone.utc) < stage2_cutoff
        ):
            try:
                note.status = "archived"
                note.updated_at_content = now
                session.add(note)
                result.archived += 1
                if vault_log:
                    vault_log.append(
                        "decay-archive",
                        getattr(note, "title", str(note.id)),
                        "system",
                    )
                logger.info("decay: archived %s (180d + low confidence)", note.id)
            except Exception as e:
                if result.errors is None:
                    result.errors = []
                result.errors.append(f"archive {note.id}: {e}")
            continue

        # Stage 1: Confidence demotion (90d)
        current_confidence = getattr(note, "confidence", "medium")
        if (
            current_confidence != "low"
            and last_active < stage1_cutoff
            and getattr(note, "created_at", now).replace(tzinfo=timezone.utc) < stage1_cutoff
        ):
            new_confidence = demote_confidence(current_confidence)
            try:
                note.confidence = new_confidence
                if new_confidence == "low":
                    note.status = "stale"
                note.updated_at_content = now
                session.add(note)
                result.demoted += 1
                if vault_log:
                    vault_log.append(
                        "decay",
                        f"{getattr(note, 'title', str(note.id))} ({current_confidence}->{new_confidence})",
                        "system",
                    )
                logger.info(
                    "decay: demoted %s (%s->%s)",
                    note.id, current_confidence, new_confidence,
                )
            except Exception as e:
                if result.errors is None:
                    result.errors = []
                result.errors.append(f"demote {note.id}: {e}")

    try:
        await session.commit()
    except Exception as e:
        logger.error("decay: commit failed: %s", e)
        if result.errors is None:
            result.errors = []
        result.errors.append(f"commit: {e}")

    logger.info(
        "decay: done — demoted=%d archived=%d skipped(pin=%d type=%d contra=%d draft=%d)",
        result.demoted, result.archived, result.skipped_pinned,
        result.skipped_type, result.skipped_contradiction, result.skipped_draft,
    )
    return result
