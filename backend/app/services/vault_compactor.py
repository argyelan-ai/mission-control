"""Inbox-Pattern compactor for cross-agent shared writes.

Watchdog on /_inbox/ — for each envelope:
  1. Parse frontmatter (op, target, sha256, idempotency_key, agent_id)
  2. Idempotency check via Redis SET NX
  3. If target exists with same SHA → skip (dedup)
  4. If target exists with different SHA → CONFLICT → move envelope to _conflicts/, publish event
  5. Otherwise → write canonical file (strip envelope-specific frontmatter, keep canonical fields)
  6. Unlink envelope (consumed)

Why this design:
- Single-writer on canonical paths (compactor is the only writer outside agent-owned folders)
- Race condition between two agents writing same target → first wins canonical, second goes to _conflicts/
- Idempotency keys allow safe retries (e.g. agent crash + restart)
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.services.vault_cache import publish_vault_event

logger = logging.getLogger("mc.vault_compactor")


class VaultCompactor:
    # Frontmatter keys that belong to the envelope envelope only — stripped from canonical
    ENVELOPE_ONLY_KEYS = {"op", "target", "idempotency_key", "sha256", "agent_id"}

    def __init__(self, vault_path: Path, redis: Any, qdrant: Any = None, spark: Any = None):
        self.vault = vault_path
        self.redis = redis
        self._qdrant = qdrant
        self._spark = spark
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        inbox = self.vault / "_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (self.vault / "_conflicts").mkdir(exist_ok=True)
        handler = _InboxHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(inbox), recursive=False)
        self._observer.start()
        logger.info("VaultCompactor watching %s", inbox)

    async def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            logger.info("VaultCompactor stopped")

    async def compact(self) -> dict[str, int]:
        """Process all envelopes currently in _inbox/.

        Returns stats: {processed, written, deduped, conflicted, malformed}.
        """
        stats = {"processed": 0, "written": 0, "deduped": 0, "conflicted": 0, "malformed": 0}
        inbox = self.vault / "_inbox"
        if not inbox.exists():
            return stats
        for env_path in sorted(inbox.glob("*.md")):
            stats["processed"] += 1
            result = await self._process_envelope(env_path)
            stats[result] = stats.get(result, 0) + 1
        return stats

    async def _process_envelope(self, env_path: Path) -> str:
        """Process single envelope. Returns one of: written, deduped, conflicted, malformed."""
        try:
            envelope = frontmatter.load(str(env_path))
            meta = envelope.metadata

            target_rel = meta.get("target")
            if not target_rel:
                logger.warning("Envelope %s missing 'target' — skipping (left in _inbox/)", env_path)
                return "malformed"

            idempotency_key = meta.get("idempotency_key")
            new_sha = meta.get("sha256", "")

            # Idempotency check via Redis SET NX
            if idempotency_key:
                redis_key = f"mc:vault:compactor:idem:{idempotency_key}"
                acquired = await self.redis.set(redis_key, new_sha, nx=True, ex=86400)
                if not acquired:
                    logger.info("Dedup: idempotency_key %s already processed", idempotency_key)
                    env_path.unlink()
                    return "deduped"

            target = self.vault / target_rel
            if target.exists():
                from hashlib import sha256 as _sha
                existing_bytes = target.read_bytes()
                # If frontmatter has a sha matching body of existing canonical → it's actually a dedup (someone wrote same content via another path)
                # For now: any difference = conflict.
                # NOTE: we compare ENTIRE-file SHA of canonical vs body SHA of envelope — these have different content (frontmatter included for canonical, body-only for envelope sha256 field).
                # So we must compute SHA the same way for both. The cleanest: SHA of body only.
                existing_body = self._extract_body(existing_bytes)
                existing_body_sha = _sha(existing_body.encode()).hexdigest()
                if existing_body_sha == new_sha:
                    # Same body content → dedup, not conflict
                    logger.info("Same body SHA at target %s — dedup", target_rel)
                    env_path.unlink()
                    return "deduped"
                # Different body → CONFLICT
                return await self._conflict(env_path, target, envelope, target_rel)

            # Write canonical
            target.parent.mkdir(parents=True, exist_ok=True)
            canonical_fm = {k: v for k, v in meta.items() if k not in self.ENVELOPE_ONLY_KEYS}
            # Phase 2: new canonical notes start as draft
            canonical_fm["status"] = "draft"
            canonical = frontmatter.Post(envelope.content, **canonical_fm)
            target.write_text(frontmatter.dumps(canonical))
            env_path.unlink()
            # publish_vault_event = bump graph-cache version + broadcast.
            await publish_vault_event(
                self.redis,
                {"type": "compacted", "path": target_rel, "from_envelope": env_path.name},
            )
            logger.info("Compacted envelope %s → %s", env_path.name, target_rel)

            # Phase 2: schedule promotion + contradiction check (fail-soft)
            try:
                from app.services.vault_promoter import VaultPromoter
                promoter = VaultPromoter(self.vault, self.redis)
                note_id = canonical_fm.get("id", "")
                await promoter.schedule_promotion(note_id, target_rel)
            except Exception as exc:
                logger.warning("Compactor: promotion scheduling failed for %s: %s", target_rel, exc)

            try:
                from app.services.vault_contradiction import check_contradictions
                # Only run if Qdrant + Spark are available (fail-soft)
                qdrant = getattr(self, "_qdrant", None)
                spark = getattr(self, "_spark", None)
                if qdrant and spark:
                    results = await check_contradictions(target, self.vault, qdrant, spark)
                    if results:
                        # Flag contradictions in frontmatter
                        import frontmatter as _fm
                        post = _fm.load(str(target))
                        contradiction_ids = [r.other_note_id for r in results if r.relation == "contradicts"]
                        if contradiction_ids:
                            post.metadata["contradiction_ids"] = contradiction_ids
                            post.metadata["confidence"] = "low"
                            target.write_text(_fm.dumps(post))
                            logger.info("Compactor: %d contradictions flagged for %s", len(contradiction_ids), target_rel)
            except Exception as exc:
                logger.warning("Compactor: contradiction check failed for %s: %s", target_rel, exc)


            return "written"

        except Exception as e:
            logger.error("Compactor error on %s: %s", env_path, e, exc_info=True)
            return "malformed"

    async def _conflict(self, env_path: Path, target: Path, envelope, target_rel: str) -> str:
        conflicts = self.vault / "_conflicts"
        conflicts.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = conflicts / f"{ts}_{env_path.name}"
        shutil.move(str(env_path), str(dest))
        await self.redis.publish(
            "vault:stream",
            json.dumps({
                "type": "conflict",
                "envelope": dest.name,
                "target": target_rel,
                "agent": envelope.metadata.get("agent_id"),
            }),
        )
        logger.warning(
            "Conflict: envelope %s vs canonical %s — moved to _conflicts/%s",
            env_path.name, target_rel, dest.name,
        )
        return "conflicted"

    @staticmethod
    def _extract_body(file_bytes: bytes) -> str:
        """Extract body (markdown content) from a file, stripping frontmatter."""
        try:
            post = frontmatter.loads(file_bytes.decode("utf-8"))
            return post.content
        except Exception:
            return file_bytes.decode("utf-8", errors="replace")


class _InboxHandler(FileSystemEventHandler):
    """Watchdog → asyncio bridge for inbox events."""

    def __init__(self, compactor: VaultCompactor):
        self.compactor = compactor

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._schedule(Path(event.dest_path))

    def _schedule(self, path: Path):
        loop = self.compactor._loop
        if loop is None or loop.is_closed():
            return
        fut = asyncio.run_coroutine_threadsafe(
            self.compactor._process_envelope(path),
            loop,
        )
        fut.add_done_callback(self._log_unhandled)

    @staticmethod
    def _log_unhandled(fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.error("Unhandled error in compactor handler: %r", exc, exc_info=exc)
