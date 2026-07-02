"""Redis sortedset activity tracking for vault notes — feeds the
ActivityHeatmap in the 3D-Graph. Rolling 30d window via TTL on the keys.

Key schemas:
  mc:vault:views:30d  → sortedset (path → view-count)
  mc:vault:writes:30d → sortedset (path → write-count from system events)
"""

from typing import Any
from redis.asyncio import Redis


VIEWS_KEY = "mc:vault:views:30d"
WRITES_KEY = "mc:vault:writes:30d"
TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
VIEW_QUEUE_KEY = "mc:vault:view_queue"

# Backward-compat alias for one release
SORTEDSET_KEY = VIEWS_KEY


class VaultActivity:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def track_view(self, path: str, user_id: str | None = None) -> None:
        """User/agent READ the note — increments view heatmap. Sliding 30d window."""
        await self.redis.zincrby(VIEWS_KEY, 1, path)
        await self.redis.expire(VIEWS_KEY, TTL_SECONDS)

    async def track_write(self, path: str, source: str | None = None) -> None:
        """System WROTE the note (watcher event, migration, etc.). Separate sortedset
        so writes don't pollute the read-heatmap shown in the 3D Graph."""
        await self.redis.zincrby(WRITES_KEY, 1, path)
        await self.redis.expire(WRITES_KEY, TTL_SECONDS)

    async def top_n_views(self, limit: int = 50, window: str = "30d") -> list[dict[str, Any]]:
        """Most-read notes — feeds Graph heatmap."""
        raw = await self.redis.zrevrange(VIEWS_KEY, 0, limit - 1, withscores=True)
        return [{"path": p, "score": s} for p, s in raw]

    async def top_n_writes(self, limit: int = 50, window: str = "30d") -> list[dict[str, Any]]:
        """Most-modified notes — for audit/observability, not heatmap."""
        raw = await self.redis.zrevrange(WRITES_KEY, 0, limit - 1, withscores=True)
        return [{"path": p, "score": s} for p, s in raw]


    async def enqueue_view_for_db(self, note_id: str, path: str | None = None) -> None:
        """Enqueue a note ID for batch DB update of last_viewed_at.

        Also tracks the Redis heatmap (if path is provided) for the 3D Graph.
        A background worker flushes the queue every 60s into the DB.
        """
        await self.redis.lpush(VIEW_QUEUE_KEY, note_id)
        if path:
            await self.track_view(path)

    async def flush_view_queue(self, batch_size: int = 200) -> list[str]:
        """Read and drain up to batch_size note IDs from the view queue.

        Returns the list of note IDs. The caller is responsible for
        updating last_viewed_at in the DB for each ID.

        Uses a Redis pipeline so LRANGE + LTRIM execute atomically
        (no entries lost if new items arrive between the two commands).
        """
        pipe = self.redis.pipeline()
        pipe.lrange(VIEW_QUEUE_KEY, 0, batch_size - 1)
        pipe.ltrim(VIEW_QUEUE_KEY, batch_size, -1)
        results = await pipe.execute()
        raw = results[0]

        if not raw:
            return []

        return [
            entry.decode("utf-8") if isinstance(entry, bytes) else str(entry)
            for entry in raw
        ]


    # Backward-compat alias (deprecated, will be removed in M.5)
    async def top_n(self, limit: int = 50, window: str = "30d") -> list[dict[str, Any]]:
        return await self.top_n_views(limit=limit, window=window)
