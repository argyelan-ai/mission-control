"""Vault Note Promoter — draft -> published after 24h.

Uses a Redis sorted set as a delayed queue:
  mc:vault:promote:queue — sorted set, score = due_at (Unix timestamp)

The check_pending_promotions() method is called periodically by a
background task (wired in main.py). It queries the sorted set for
entries whose score (due_at) <= now and promotes them.

Design: fail-soft everywhere. A missed promotion is caught on the
next cycle. A down Redis/DB delays promotion but doesn't lose data.
"""

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

logger = logging.getLogger("mc.vault_promoter")

PROMOTE_DELAY_SECONDS = 86400  # 24 hours
PROMOTE_QUEUE_KEY = "mc:vault:promote:queue"
# Legacy prefix kept for cleanup of pre-migration keys
PROMOTE_KEY_PREFIX = "mc:vault:promote:"


class VaultPromoter:
    def __init__(self, vault_path: Path, redis: Any):
        self.vault = vault_path
        self.redis = redis

    async def schedule_promotion(self, note_id: str, rel_path: str) -> None:
        """Schedule a note for auto-promotion after PROMOTE_DELAY_SECONDS.

        Adds an entry to a Redis sorted set with score = due_at timestamp.
        check_pending_promotions() queries for entries where score <= now.
        """
        due_at = time.time() + PROMOTE_DELAY_SECONDS
        payload = json.dumps({"note_id": note_id, "rel_path": rel_path})
        await self.redis.zadd(PROMOTE_QUEUE_KEY, {payload: due_at})
        logger.info("Promotion scheduled: %s -> %s (24h, due_at=%.0f)", note_id, rel_path, due_at)

    async def promote_note(
        self,
        note_id: str,
        rel_path: str,
        *,
        db_session: Any = None,
        force: bool = False,
    ) -> bool:
        """Promote a note from draft to published.

        Returns True if promoted, False if blocked or note not found.

        When force=True, promotes even if contradiction_ids is non-empty
        (used by admin manual-promote endpoint).
        """
        key = f"{PROMOTE_KEY_PREFIX}{note_id}"
        full = self.vault / rel_path

        if not full.exists():
            logger.warning("Promote: note %s not found at %s — cleaning stale key", note_id, rel_path)
            await self.redis.delete(key)
            return False

        try:
            post = frontmatter.load(str(full))
        except Exception as exc:
            logger.error("Promote: cannot parse %s: %s", rel_path, exc)
            await self.redis.delete(key)
            return False

        if post.metadata.get("status") != "draft":
            logger.info("Promote: %s is already %s — skipping", note_id, post.metadata.get("status"))
            await self.redis.delete(key)
            return False

        # Check for unresolved contradictions
        contradiction_ids = post.metadata.get("contradiction_ids") or []
        if contradiction_ids and not force:
            logger.info(
                "Promote: %s blocked by %d contradictions — not promoting",
                note_id, len(contradiction_ids),
            )
            return False

        # Update frontmatter on disk
        post.metadata["status"] = "published"
        post.metadata["updated"] = datetime.now(timezone.utc).isoformat()
        full.write_text(frontmatter.dumps(post))

        # Update DB (best-effort) — vault file is source of truth
        if db_session is not None:
            try:
                import uuid as _uuid
                from sqlmodel import select
                from app.models.memory import BoardMemory
                stmt = select(BoardMemory).where(
                    BoardMemory.id == _uuid.UUID(note_id)
                ) if note_id else None
                if stmt is not None:
                    row = (await db_session.execute(stmt)).scalar_one_or_none()
                    if row:
                        row.status = "published"
                        row.confidence = row.confidence or "medium"
                        await db_session.commit()
            except (ValueError, Exception) as exc:
                # ValueError: note_id is not a valid UUID — skip DB update
                logger.warning("Promote: DB update failed for %s: %s", note_id, exc)

        # Publish event
        try:
            import json
            await self.redis.publish(
                "vault:stream",
                json.dumps({"type": "promoted", "path": rel_path, "note_id": note_id}),
            )
        except Exception:
            pass

        await self.redis.delete(key)
        logger.info("Promoted: %s (%s) -> published", note_id, rel_path)
        return True

    async def reject_note(self, note_id: str, rel_path: str) -> bool:
        """Move a note to _rejected/ and clean up its promotion timer.

        Returns True if rejected, False if note not found.
        """
        full = self.vault / rel_path
        if not full.exists():
            logger.warning("Reject: note %s not found at %s", note_id, rel_path)
            return False

        rejected_dir = self.vault / "_rejected"
        rejected_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_rel = rel_path.replace("/", "__")
        dest = rejected_dir / f"{ts}-{safe_rel}"
        shutil.move(str(full), str(dest))

        # Clean up promotion timer
        key = f"{PROMOTE_KEY_PREFIX}{note_id}"
        await self.redis.delete(key)

        # Publish event
        try:
            import json
            await self.redis.publish(
                "vault:stream",
                json.dumps({"type": "rejected", "path": rel_path, "note_id": note_id}),
            )
        except Exception:
            pass

        logger.info("Rejected: %s -> %s", rel_path, dest.name)
        return True

    async def check_pending_promotions(self, db_session: Any = None) -> int:
        """Poll for notes ready to be promoted.

        Queries the Redis sorted set for entries whose score (due_at)
        is <= now. For each due entry, promotes the note and removes
        it from the queue.

        This method is meant to be called from a periodic background task
        (e.g., every 5 minutes). The actual 24h delay is enforced by the
        sorted-set score — entries only become eligible when their
        due_at timestamp has passed.

        Returns the number of notes promoted.
        """
        promoted = 0
        now = time.time()
        try:
            due_entries = await self.redis.zrangebyscore(PROMOTE_QUEUE_KEY, 0, now)
        except Exception as exc:
            logger.warning("check_pending_promotions: Redis zrangebyscore failed: %s", exc)
            return 0

        for entry in due_entries:
            try:
                raw = entry.decode() if isinstance(entry, bytes) else entry
                data = json.loads(raw)
                note_id = data["note_id"]
                rel_path = data["rel_path"]

                if await self.promote_note(note_id, rel_path, db_session=db_session):
                    promoted = promoted + 1

                # Remove from queue regardless of promote success (avoid infinite retry)
                await self.redis.zrem(PROMOTE_QUEUE_KEY, entry)

            except Exception as exc:
                logger.warning("check_pending_promotions: error processing entry: %s", exc)
                # Still remove malformed entries to avoid infinite retry
                try:
                    await self.redis.zrem(PROMOTE_QUEUE_KEY, entry)
                except Exception:
                    pass

        if promoted > 0:
            logger.info("check_pending_promotions: promoted %d notes", promoted)
        return promoted
