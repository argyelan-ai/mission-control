"""Vault Watcher — filesystem event handler.

Read-only mode for M.1: watches the vault, validates new/modified files,
dispatches to index/embeddings/activity/git/redis-stream.

Does NOT handle _inbox/ — that's vault_compactor.py (M.2).

Path-Ownership Rule:
  Files under /vault/agents/{slug}/ must have frontmatter agent == slug.
  Files under /vault/global/ or /vault/projects/{p}/ can be from any agent.

Quarantine:
  Invalid frontmatter / path-traversal violations → /vault/_rejected/
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

from app.helpers.vault_constants import EXCLUDED_PREFIXES
from app.helpers.vault_frontmatter import (
    parse_frontmatter,
    validate_frontmatter,
    FrontmatterError,
)
from app.services.vault_cache import publish_vault_event

logger = logging.getLogger("mc.vault_watcher")

# 30s batch-commit interval for VaultGit. Named constant so tests can pin
# the wiring without waiting a wall-clock minute (and so the interval is
# greppable next to the loop that consumes it).
COMMIT_INTERVAL_SECONDS = 30
# Grace period when draining in-flight handler coroutines on shutdown.
# Watchdog handlers do parse/frontmatter/index/embeddings work; a batch
# commit p95 is ~54ms (measured, PR #614), so a few seconds covers
# pathological cases without stalling teardown.
SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 5.0


class VaultWatcher:
    def __init__(
        self,
        vault_path: Path,
        index: Any,
        activity: Any,
        embeddings: Any,
        git: Any,
        redis: Any,
    ):
        self.vault = vault_path
        self.index = index
        self.activity = activity
        self.embeddings = embeddings
        self.git = git
        self.redis = redis
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # In-flight handler futures (from _schedule) — drained on shutdown
        # so a write racing the restart is not lost (Rex finding 2).
        self._inflight: set = set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.vault.mkdir(parents=True, exist_ok=True)
        (self.vault / "_rejected").mkdir(exist_ok=True)
        handler = _Handler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.vault), recursive=True)
        self._observer.start()
        # 30s batch timer — the module contract of VaultGit ("real git
        # add/commit with 30s batching") was never wired: stage() alone
        # accumulates the index without ever committing. flush_if_pending
        # holds VaultGit's lock, so overlapping ticks are serialized.
        self._commit_task = asyncio.create_task(self._commit_loop())

    async def stop(self) -> None:
        task = getattr(self, "_commit_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Drain in-flight handler coroutines before the final flush.
        # _schedule() hands work to run_coroutine_threadsafe futures; a
        # shutdown racing an in-flight handler (embeddings/index work can
        # take hundreds of ms) used to lose that note's git.stage() — the
        # final flush below would run on an empty index and the write
        # would never be committed. Await pending futures (bounded) so the
        # fleet's last write survives a restart.
        pending = [f for f in self._inflight if not f.done()]
        if pending:
            try:
                # run_coroutine_threadsafe yields concurrent.futures.Futures —
                # wrap them so asyncio.wait can await them on this loop.
                wrapped = [asyncio.wrap_future(f) for f in pending]
                await asyncio.wait(wrapped, timeout=SHUTDOWN_DRAIN_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 — shutdown must not raise
                logger.warning("Vault shutdown drain failed", exc_info=True)
        self._inflight.clear()
        # Final flush on shutdown so the last writes don't wait 30s in limbo.
        try:
            await self.git.flush_if_pending("shutdown")
        except Exception:  # noqa: BLE001 — shutdown must not raise
            logger.warning("Final vault commit on shutdown failed", exc_info=True)
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            logger.info("VaultWatcher stopped")

    async def _commit_loop(self) -> None:
        while True:
            await asyncio.sleep(COMMIT_INTERVAL_SECONDS)
            try:
                await self.git.flush_if_pending("batched")
            except Exception:  # noqa: BLE001 — keep the loop alive on git hiccups
                logger.warning("Vault batch commit failed", exc_info=True)

    def _is_excluded(self, file_path: Path) -> bool:
        rel = str(file_path.relative_to(self.vault))
        return any(rel.startswith(p) for p in EXCLUDED_PREFIXES) or not rel.endswith(".md")

    def _validate_path_ownership(self, file_path: Path, post: frontmatter.Post) -> bool:
        rel = file_path.relative_to(self.vault)
        parts = rel.parts
        if len(parts) >= 3 and parts[0] == "agents":
            owner_slug = parts[1]
            frontmatter_agent = post.metadata.get("agent")
            if frontmatter_agent != owner_slug:
                logger.warning(
                    "Path-ownership violation: %s has agent=%s in frontmatter",
                    rel, frontmatter_agent,
                )
                return False
        return True

    async def _handle_create_or_modify(self, file_path: Path) -> None:
        if not file_path.exists():
            return  # Race: file was deleted between event and handler
        if self._is_excluded(file_path):
            return

        try:
            post = parse_frontmatter(file_path)
            validate_frontmatter(post.metadata)
        except FrontmatterError as e:
            return await self._quarantine(file_path, reason=str(e))

        if not self._validate_path_ownership(file_path, post):
            return await self._quarantine(file_path, reason="path-ownership-violation")

        # Dispatch in parallel — but block until index is updated (consistency)
        self.index.upsert(file_path, post)
        await self.embeddings.upsert(file_path, post, vault_path=self.vault)
        await self.activity.track_write(str(file_path.relative_to(self.vault)), source="watcher")
        self.git.stage(file_path)
        # publish_vault_event = bump graph-cache version + publish — done in one
        # call so the next /vault/graph request misses the stale cache.
        await publish_vault_event(
            self.redis,
            {"type": "modified", "path": str(file_path.relative_to(self.vault))},
        )

    async def _quarantine(self, file_path: Path, reason: str) -> None:
        rejected_dir = self.vault / "_rejected"
        rejected_dir.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = rejected_dir / f"{ts}_{file_path.name}"
        try:
            shutil.move(str(file_path), str(dest))
        except Exception as e:
            logger.error("Cannot move %s → _rejected/: %s", file_path, e)
            return
        logger.warning("Quarantined %s → _rejected/ — reason: %s", file_path, reason)


class _Handler(FileSystemEventHandler):
    """Bridges watchdog (sync) to asyncio."""

    def __init__(self, watcher: VaultWatcher):
        self.watcher = watcher

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(Path(event.src_path))

    def on_moved(self, event):
        """Handles atomic writes via os.replace() — Linux inotify only fires MOVED, not CREATED/MODIFIED."""
        if not event.is_directory:
            # event.dest_path is the new location (where the file landed)
            self._schedule(Path(event.dest_path))

    def _schedule(self, path: Path):
        loop = self.watcher._loop
        if loop is None or loop.is_closed():
            return
        fut = asyncio.run_coroutine_threadsafe(
            self.watcher._handle_create_or_modify(path),
            loop,
        )
        self.watcher._inflight.add(fut)
        fut.add_done_callback(self._log_unhandled)
        fut.add_done_callback(lambda f, w=self.watcher: w._inflight.discard(f))

    @staticmethod
    def _log_unhandled(fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.error("Unhandled error in vault handler: %r", exc, exc_info=exc)
