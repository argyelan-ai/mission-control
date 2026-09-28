"""E0 page usage — route beacon (docs/metrics/2026-09-baseline.md §2.4).

No existing log holds page views (baseline §2.1: the proxy writes no access
log, the backend log has API paths only), so the frontend reports each route
change and we keep one counter per day and route pattern. Nothing else is
stored: no user, no ids, no query string, no timestamp finer than a day.

Storage is a Redis hash per UTC day (``mc:usage:page:<YYYY-MM-DD>``, field =
route pattern) — Redis runs with AOF persistence, so this needs no table and
no migration. Keys expire after ~13 months.

This is also the page half of the "beacon" planned for stage E5 (coherence
concept C7: counters per day and ID); a later ``data-feature`` click counter
can use the same shape under its own key prefix.
"""

import re
from datetime import datetime, timedelta, timezone

from app.services.usage_baseline import MAX_WEEKS, iso_week_label, week_starts
from app.utils import ensure_aware, utcnow

KEY_PREFIX = "mc:usage:page:"
DROPPED_FIELD = "__dropped__"
KEY_TTL_SECONDS = 400 * 24 * 3600
# Guard against a misbehaving client filling a day's hash: the app has ~25
# route patterns; anything past this many distinct routes a day is only counted
# as dropped.
MAX_ROUTES_PER_DAY = 100

_MAX_SEGMENTS = 4
_MAX_LEN = 64
_SEGMENT = re.compile(r"^(?::[a-z]+|[a-z0-9][a-z0-9-]*)$")
_ID_LIKE = re.compile(
    r"^(?:\d+|[0-9a-f]{12,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def normalize_route(raw: str) -> str | None:
    """Route pattern for ``raw``, or None when it is not a plain app path.

    Query and fragment are dropped, id-like segments become ``:id``, and
    anything that is not lower-case letters, digits and dashes is rejected —
    so no free text can end up in the counters.
    """
    if not raw or not raw.startswith("/"):
        return None
    path = re.split(r"[?#]", raw, maxsplit=1)[0].lower().rstrip("/")
    if not path:
        return "/"
    if len(path) > _MAX_LEN:
        return None
    segments = path.split("/")[1:]
    if len(segments) > _MAX_SEGMENTS:
        return None
    out = []
    for seg in segments:
        if _ID_LIKE.match(seg):
            out.append(":id")
        elif seg.startswith(":"):
            out.append(":id")
        elif _SEGMENT.match(seg):
            out.append(seg)
        else:
            return None
    return "/" + "/".join(out)


def _day_key(day) -> str:
    return f"{KEY_PREFIX}{day.isoformat()}"


async def record_page_view(redis, route: str, *, now: datetime | None = None) -> None:
    """Count one view of an already normalised ``route`` for today (UTC)."""
    now = ensure_aware(now) if now is not None else utcnow()
    key = _day_key(now.astimezone(timezone.utc).date())
    known = await redis.hexists(key, route)
    if not known and await redis.hlen(key) - int(await redis.hexists(key, DROPPED_FIELD)) >= MAX_ROUTES_PER_DAY:
        route = DROPPED_FIELD
    await redis.hincrby(key, route, 1)
    await redis.expire(key, KEY_TTL_SECONDS)


async def page_views_by_week(redis, *, weeks: int = 6, now: datetime | None = None) -> dict:
    """Views per ISO week × route pattern, oldest week first."""
    now = ensure_aware(now) if now is not None else utcnow()
    out = []
    for monday in week_starts(min(weeks, MAX_WEEKS), now):
        routes: dict[str, int] = {}
        dropped = 0
        for offset in range(7):
            counts = await redis.hgetall(_day_key(monday + timedelta(days=offset)))
            for route, n in counts.items():
                if route == DROPPED_FIELD:
                    dropped += int(n)
                else:
                    routes[route] = routes.get(route, 0) + int(n)
        routes = dict(sorted(routes.items(), key=lambda kv: kv[1], reverse=True))
        out.append({
            "week": iso_week_label(monday),
            "week_start": monday.isoformat(),
            "total": sum(routes.values()),
            "dropped": dropped,
            "routes": routes,
        })
    return {"generated_at": now.isoformat(), "weeks": out}
