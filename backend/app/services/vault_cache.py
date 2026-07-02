"""Redis-backed cache for the vault graph response.

The graph build path (``vault_graph.build_graph``) is the slowest read in
the vault subsystem — it scrolls every embedding out of Qdrant, runs
k-means with silhouette scoring, and optionally performs N nearest-
neighbour queries for "ghost" similarity edges. On a 320-note vault that
adds up to 1.5-4 seconds of cold-path latency.

Caching strategy: **versioned key**, no manual eviction.

  - A single integer counter at ``mc:vault:graph:version`` is bumped
    every time the vault changes (note upsert, delete, restore, purge,
    conflict, compaction).
  - Cache entries live at
    ``mc:vault:graph:cache:v<version>:<params-hash>``.
  - After a bump, the next request misses the cache (new version key
    doesn't exist yet) and rebuilds. Older version keys age out via TTL.

The TTL exists purely as a safety net — if the version counter ever
got out of sync with reality (e.g. an external process touched files
behind the watcher's back), stale entries auto-expire within minutes
instead of pinning forever.

Why a version counter beats DEL-on-event: avoids races between
"DEL fired" and "subsequent rebuild reads stale data" — once the
counter increments, every reader sees the new key space immediately,
and no rebuild can write to the old namespace.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("mc.vault_cache")

_VERSION_KEY = "mc:vault:graph:version"
_CACHE_PREFIX = "mc:vault:graph:cache:"
_CACHE_TTL_SECONDS = 600  # 10 min safety net


def params_hash(*, cluster: bool, heatmap: str, similarity_edges: bool) -> str:
    """Stable hex digest of the params that affect graph output."""
    raw = json.dumps(
        {
            "cluster": bool(cluster),
            "heatmap": str(heatmap),
            "similarity_edges": bool(similarity_edges),
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


async def get_graph_version(redis: Any) -> int:
    """Read the current vault graph version. Returns 0 on a fresh Redis."""
    try:
        v = await redis.get(_VERSION_KEY)
        if v is None:
            return 0
        return int(v)
    except Exception as e:
        logger.warning("vault_cache: version read failed: %s", e)
        return 0


async def bump_graph_version(redis: Any) -> int:
    """Increment the vault graph version. New value returned."""
    try:
        new_v = int(await redis.incr(_VERSION_KEY))
        return new_v
    except Exception as e:
        logger.warning("vault_cache: version bump failed: %s", e)
        return 0


def _cache_key(version: int, p_hash: str) -> str:
    return f"{_CACHE_PREFIX}v{version}:{p_hash}"


async def get_cached_graph(redis: Any, version: int, p_hash: str) -> Optional[dict[str, Any]]:
    """Return cached graph payload or None on miss / error."""
    try:
        raw = await redis.get(_cache_key(version, p_hash))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        logger.warning("vault_cache: read failed: %s", e)
        return None


async def set_cached_graph(
    redis: Any, version: int, p_hash: str, payload: dict[str, Any]
) -> None:
    """Persist graph payload to cache. Fail-soft."""
    try:
        await redis.set(
            _cache_key(version, p_hash),
            json.dumps(payload),
            ex=_CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning("vault_cache: write failed: %s", e)


async def publish_vault_event(redis: Any, event: dict[str, Any]) -> None:
    """Bump the graph version AND broadcast the event to vault:stream.

    Single-call helper so callers don't forget either step. The version
    bump invalidates every cached graph payload, the publish wakes up
    every connected client (browser tabs, voice agent) so they refetch.
    """
    try:
        await bump_graph_version(redis)
    except Exception:
        pass
    try:
        await redis.publish("vault:stream", json.dumps(event))
    except Exception as e:
        logger.warning(
            "vault_cache: publish failed for event %s: %s",
            event.get("type", "?"),
            e,
        )
