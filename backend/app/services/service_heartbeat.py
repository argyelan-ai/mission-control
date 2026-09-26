"""Cross-process liveness for background loops (watchdog, task runner).

Since the worker container split, the background loops run in the worker
process while ``/api/v1/system/status`` is served by the API process. The
API cannot see the worker's in-process flags, nor its container-local
``/tmp/mc-worker.alive`` file. Each loop therefore writes a small Redis
heartbeat on every tick; the status endpoint reads it.

The key carries a long TTL on purpose: a heartbeat that is merely *old*
reads as "stale" (the loop hangs or the worker died recently), one that is
gone reads as "stopped". A short TTL would hide the stale state.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from app.redis_client import RedisKeys, get_redis
from app.utils import ensure_aware, utcnow

logger = logging.getLogger(__name__)

# How long a heartbeat survives without refresh before it reads as "stopped".
HEARTBEAT_KEY_TTL_SECONDS = 3600


async def record_beat(service: str, **extra: Any) -> None:
    """Write the heartbeat for ``service``. Never raises — a Redis hiccup
    must not break the loop that calls it."""
    payload = {"at": utcnow().isoformat(), "pid": os.getpid(), **extra}
    try:
        redis = await get_redis()
        await redis.set(
            RedisKeys.service_heartbeat(service),
            json.dumps(payload, default=str),
            ex=HEARTBEAT_KEY_TTL_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — liveness write is best-effort
        logger.debug("heartbeat write for %s failed: %s", service, e)


async def read_beat(service: str) -> dict | None:
    """Return the parsed heartbeat or ``None`` (missing, unreadable, Redis down)."""
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.service_heartbeat(service))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        beat = json.loads(raw)
        datetime.fromisoformat(beat["at"])
    except (ValueError, TypeError, KeyError):
        return None
    return beat if isinstance(beat, dict) else None


async def service_status(
    service: str, *, local_running: bool, stale_after_seconds: float
) -> tuple[dict, dict | None]:
    """Resolve the honest status of one background loop.

    Returns ``(component, beat)``: ``component`` holds ``status``
    ("running" | "stale" | "stopped"), ``source`` ("local" | "worker" |
    None) and ``last_seen``; ``beat`` is the raw heartbeat for callers that
    want to surface extra fields.
    """
    beat = await read_beat(service)
    last_seen = beat["at"] if beat else None
    if local_running:
        return {"status": "running", "source": "local", "last_seen": last_seen}, beat
    if beat is None:
        return {"status": "stopped", "source": None, "last_seen": None}, None
    age = (utcnow() - ensure_aware(datetime.fromisoformat(beat["at"]))).total_seconds()
    if age <= stale_after_seconds:
        return {"status": "running", "source": "worker", "last_seen": last_seen}, beat
    return {"status": "stale", "source": "worker", "last_seen": last_seen}, beat
