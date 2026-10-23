"""Is the box itself awake? — the host check, apart from its model port.

Round-4 user test (t3 S1, r2 S1): a power-managed box that was awake (its
control server answered, ``GET /hosts/<id>/metrics`` said ``awake: true``)
but ran no model read "asleep or off" in the Models tab, because the catalog
took the runtime watcher's verdict on the MODEL port as the state of the BOX.
A dark model port only says no engine answers there. Whether the box sleeps
is a question for the box — this module asks it, cheaply:

- ``local``                → the MC host itself, always awake.
- a fresh node-agent beat  → awake (the box pushed a heartbeat < 60 s ago).
- ``flask_wol``            → its control server's ``/health``; the answer is
  remembered for a few seconds, and the host card's 5 s metrics poll
  (``GET /hosts/<id>/metrics``) keeps that memory warm.
- anything else            → unknown (``None``) — an SSH round trip is too
  slow for a page that must not wait.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from app.models.host import Host
from app.redis_client import RedisKeys, get_redis
from app.utils import ensure_aware, utcnow

logger = logging.getLogger(__name__)

#: How long one control-server answer stands in for the box. The host card
#: polls every 5 s while the Runtimes page is open, so the memory is warm
#: whenever someone looks; short enough that a box which just fell asleep
#: (or woke) is read right on the next page.
AWAKE_TTL_S = 20

#: One /health on the LAN answers in milliseconds; a sleeping box drops the
#: packets, so the limit is what a sleeping box costs the page once.
PROBE_TIMEOUT_S = 1.5

#: Same tolerance as runtime_manager's telemetry freshness (15 s beat, 3 missed).
HEARTBEAT_FRESH_S = 60


async def remember(host_id: uuid.UUID | str, awake: bool) -> None:
    """Store a host check that someone already made (the metrics endpoint)."""
    try:
        redis = await get_redis()
        await redis.set(RedisKeys.host_awake(str(host_id)), "1" if awake else "0", ex=AWAKE_TTL_S)
    except Exception as exc:  # noqa: BLE001 — Redis down must not break the caller
        logger.debug("host liveness: remember %s failed (%s)", host_id, exc)


async def recall(host_id: uuid.UUID | str) -> bool | None:
    try:
        redis = await get_redis()
        raw = await redis.get(RedisKeys.host_awake(str(host_id)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("host liveness: recall %s failed (%s)", host_id, exc)
        return None
    if raw is None:
        return None
    return str(raw) == "1"


async def _probe_control_server(control_url: str) -> bool:
    url = control_url.strip().rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except Exception:  # noqa: BLE001 — a sleeping box is an answer, not an error
        return False


def _heartbeat_fresh(host: Host) -> bool:
    if host.agent_last_seen_at is None:
        return False
    age_s = (utcnow() - ensure_aware(host.agent_last_seen_at)).total_seconds()
    return age_s < HEARTBEAT_FRESH_S


async def host_awake(host: Host) -> bool | None:
    """True = the box answers, False = it does not, None = not known cheaply."""
    if host.kind == "local":
        return True
    if _heartbeat_fresh(host):
        return True
    if host.kind == "flask_wol" and host.control_url:
        remembered = await recall(host.id)
        if remembered is not None:
            return remembered
        awake = await _probe_control_server(host.control_url)
        await remember(host.id, awake)
        return awake
    return None
