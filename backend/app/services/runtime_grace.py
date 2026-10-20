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


# How long a runtime that a switch stopped on purpose stays off-limits for the
# watcher's auto-recovery. Longer than a slow cold load (≤ 30 min) — within
# that window the operator's switch wins; after it, the box's autostart rules
# apply again (e.g. the box rebooted hours later).
EVICTED_TTL = 60 * 60


async def mark_evicted(slug: str | None, *, by: str | None) -> None:
    """Remember that a start of ``by`` stopped ``slug`` to free the box.
    Best-effort — never raises."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.setex(
            RedisKeys.runtime_evicted(slug),
            EVICTED_TTL,
            json.dumps({"by": by, "at": _utcnow_iso()}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark_evicted(%s) failed: %s", slug, exc)


async def clear_evicted(slug: str | None) -> None:
    """Lift the marker — the runtime is being started again. Never raises."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.runtime_evicted(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("clear_evicted(%s) failed: %s", slug, exc)


async def get_evicted(slug: str, redis=None) -> dict | None:
    """The eviction document, or ``None`` (also when Redis is down)."""
    try:
        redis = redis or await get_redis()
        raw = await redis.get(RedisKeys.runtime_evicted(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_evicted(%s) failed: %s", slug, exc)
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


# ── Stop intent (ADR-096 §11.1) ──────────────────────────────────────────
# Every stop MC causes leaves a stop intent: who, why, since. A runtime that
# goes down WITH an intent is planned silence; one that goes down WITHOUT an
# intent is an engine death (runtime_watcher). The eviction marker above
# expires after an hour — this one does not: a runtime that was stopped or
# switched away stays "not running — on purpose" until its OWN next start
# lifts it. That is MC's start (``runtime_manager.start_runtime`` /
# ``restart_runtime``) — or a start MC only observes: ``start.sh`` by hand
# after a stop via MC (the mc-runtime-pause flow), a ``--restart always``
# container after a box reboot. The watcher lifts the intent for those once
# the row's OWN anchor runs and the port answers, at least
# ``STOP_INTENT_SETTLE`` after the stop was asked (an engine still answering
# while its stop runs is not a start). No neighbour's answer on a shared port
# and no timer ever clears it, so a manual stop can never later be read as a
# death or undone by auto-recovery. Redis runs with AOF (docker-compose:
# --appendonly yes), so "no TTL" survives a Redis restart.
INTENT_OPERATOR_STOP = "operator_stop"
INTENT_SWITCHED_AWAY = "switched_away"
INTENT_SCHEDULE = "schedule"
INTENT_IDLE_UNLOAD = "idle_unload"
INTENT_CRASH_LOOP = "crash_loop_stop"

#: How long after a stop was asked an own answer may still be the stop in
#: progress. MC's stops are bounded well below it (ssh stop 180 s + verify,
#: eviction ``docker stop -t 120``); independent of the watcher interval.
STOP_INTENT_SETTLE = 10 * 60

#: A pending start outlives every switch grace (20 min) by far — it ends with
#: the row's next answer or a stop, the TTL only collects rows nobody probes.
START_PENDING_TTL = 7 * 24 * 3600


async def mark_stop_intent(slug: str | None, why: str, *, by: str | None = None) -> None:
    """Record that MC is stopping ``slug`` on purpose. No TTL. Never raises.

    Set BEFORE the stop, so the first failed probe after it already sees it.
    A stop also ends a pending start of ``slug``: it is no longer wanted."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.set(
            RedisKeys.runtime_stop_intent(slug),
            json.dumps({"why": why, "by": by, "since": _utcnow_iso()}),
        )
        await redis.delete(RedisKeys.runtime_start_pending(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark_stop_intent(%s) failed: %s", slug, exc)


async def clear_stop_intent(slug: str | None) -> None:
    """Only a start of ``slug`` itself calls this — MC's own, or one the
    watcher observed on the box (see ``STOP_INTENT_SETTLE``). Never raises."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.runtime_stop_intent(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("clear_stop_intent(%s) failed: %s", slug, exc)


async def get_stop_intent(slug: str, redis=None) -> dict | None:
    """The intent document, or ``None``. Redis down → ``None`` (the watcher
    cannot count failures then either, so nothing is declared)."""
    try:
        redis = redis or await get_redis()
        raw = await redis.get(RedisKeys.runtime_stop_intent(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_stop_intent(%s) failed: %s", slug, exc)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"why": "unknown"}
    return doc if isinstance(doc, dict) else {"why": "unknown"}


def marker_age_seconds(doc: dict | None, field: str) -> float | None:
    """Seconds since ``doc[field]`` (an ISO time), ``None`` when unknown."""
    from datetime import datetime, timezone

    raw = (doc or {}).get(field)
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds()


# ── Pending start (ADR-096 §11.2) ────────────────────────────────────────
# MC launched ``slug`` and it has not answered since: it is the port's wanted
# holder. The switch grace (20 min) keeps the watcher quiet while it loads;
# when the grace runs out and the row still never answered, this marker is
# what makes the silence an outage again (one plain alarm) — instead of
# "a row that never served its own answer" on a shared port, which stays
# quiet. Ends with the row's next answer (watcher), a stop intent, or the TTL.
#
# A start is also a new window for the PORT: the endpoint's one-death /
# one-alarm keys of the outage before it are closed here (§11.4). Otherwise
# "Restart now" or "Other model" on a dead card that never comes up stays
# silent until those keys expire (24 h) — only an answer cleared them.


async def mark_start_pending(slug: str | None, source: str, *, endpoint: str | None) -> None:
    """``runtime_manager`` calls this where it opens the switch window.
    ``endpoint`` is the row's port (``None``: keyed by slug, as the claims
    are). Never raises."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.setex(
            RedisKeys.runtime_start_pending(slug),
            START_PENDING_TTL,
            json.dumps({"source": source, "since": _utcnow_iso()}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark_start_pending(%s) failed: %s", slug, exc)
        return
    from app.services import engine_death

    await engine_death.start_opened(redis, endpoint=endpoint, slug=slug)


async def clear_start_pending(slug: str | None, redis=None) -> None:
    """The row answered (own engine or a neighbour holding the port). Never raises."""
    if not slug:
        return
    try:
        redis = redis or await get_redis()
        await redis.delete(RedisKeys.runtime_start_pending(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("clear_start_pending(%s) failed: %s", slug, exc)


async def get_start_pending(slug: str, redis=None) -> dict | None:
    """The pending-start document, or ``None`` (also when Redis is down)."""
    try:
        redis = redis or await get_redis()
        raw = await redis.get(RedisKeys.runtime_start_pending(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_start_pending(%s) failed: %s", slug, exc)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"source": "unknown"}
    return doc if isinstance(doc, dict) else {"source": "unknown"}


async def clear_death_record(slug: str | None) -> None:
    """A start or a stop of ``slug`` acknowledges an earlier engine death: the
    card falls back from "failed — <reason>" to its normal state. The event in
    the feed stays. Never raises."""
    if not slug:
        return
    try:
        redis = await get_redis()
        await redis.delete(RedisKeys.runtime_death(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("clear_death_record(%s) failed: %s", slug, exc)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
