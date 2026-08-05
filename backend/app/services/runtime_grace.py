"""Runtime switch-grace state (PR5).

A recipe switch on the DGX Spark takes 2.5–8.5 minutes; a cold first load of a
large model 10–15. During that window the engine's ``/v1/models`` endpoint is
simply down — which the runtime watcher (ADR-054) could not tell apart from a
real outage. Result: a burst of ``runtime.unreachable`` events plus operator
notifications on every planned switch (documented incident).

This module holds the one piece of shared state that closes the gap: a Redis
key saying "this runtime is *expected* to be unreachable right now, and why".

Who writes it
-------------
- ``sparkrun_manager.switch_recipe`` — ``evicting`` before the eviction,
  then hands over to ``start_runtime``, then ``loading`` once the launch
  returned ok. Clears it on every abort path.
- ``runtime_manager.start_runtime`` / ``restart_runtime`` — ``launching`` for
  docker engine types (manual starts hit the same cold-load window), cleared
  again when the call itself reports failure.
- ``runtime_watcher`` — clears it centrally as soon as a probe reports the
  engine is serving again. That is the ONE place a *successful* switch ends,
  so no caller has to poll for readiness.

The TTL is the safety net: if the backend dies mid-switch nobody is left to
clear the key, and without an expiry the watcher would stay blind forever.
20 minutes is deliberately longer than the worst observed first load.

Distinct from ``runtime_autostart``: that one toggles a flag file that decides
whether the engine starts on *host boot*. This module is about the in-flight
window of a start MC itself triggered.

Every helper is best-effort: Redis down must never break a lifecycle op, so
failures are swallowed and the system behaves exactly as it did before PR5
(no grace, no recovery).
"""

from __future__ import annotations

import json
import logging

from app.redis_client import RedisKeys, get_redis

logger = logging.getLogger(__name__)

# Longer than the worst-case first load (10–15 min) so a legitimately slow
# start never falls out of grace early.
SWITCHING_TTL = 20 * 60

# Valid ``phase`` values, in the order a switch passes through them.
PHASE_EVICTING = "evicting"
PHASE_LAUNCHING = "launching"
PHASE_LOADING = "loading"

# Valid ``source`` values — who initiated the start.
SOURCE_SWITCH = "switch_recipe"
SOURCE_MANUAL = "manual_start"
SOURCE_AUTO_RECOVERY = "auto_recovery"


async def mark_switching(slug: str | None, phase: str, source: str) -> None:
    """Mark ``slug`` as in-flight. Best-effort — never raises."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.setex(
            RedisKeys.runtime_switching(slug),
            SWITCHING_TTL,
            json.dumps({"phase": phase, "source": source,
                        "started_at": _utcnow_iso()}),
        )
    except Exception as exc:  # noqa: BLE001 — grace is an optimisation
        logger.debug("mark_switching(%s) failed: %s", slug, exc)


async def clear_switching(slug: str | None) -> None:
    """Drop the in-flight marker. Best-effort — never raises."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.runtime_switching(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("clear_switching(%s) failed: %s", slug, exc)


async def get_switching(slug: str, redis=None) -> dict | None:
    """Return the in-flight document, or ``None`` (also when Redis is down).

    ``redis`` may be passed by callers that already hold a client (the watcher
    does) so a probe tick doesn't open a second connection.
    """
    try:
        redis = redis or await get_redis()
        raw = await redis.get(RedisKeys.runtime_switching(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_switching(%s) failed: %s", slug, exc)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
