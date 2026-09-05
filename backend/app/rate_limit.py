"""Global rate limiting — which routes it applies to, and which it must not.

Why (PR #404 review, MITTEL-1)
------------------------------
PR #404 wired `SlowAPIMiddleware` so `default_limits` finally applies to every
route. That was the right fix, but the budget was never measured under real
load, and the counting key is the client IP:

* Mark's browser is **one** IP for the whole UI — TanStack polling across many
  endpoints plus SSE reconnects — so the whole frontend shares a single bucket.
  A 429 there looks exactly like a backend outage.
* The agent fleet polls every 5s per container (docker/shared/poll.sh) and talks
  to `backend:8000` directly, bypassing Caddy. They still pass through this
  middleware, so they consume a bucket too.

So two changes: a budget with headroom (600/minute), and machine traffic plus
liveness plus long-lived streams taken out of the count entirely. What remains
limited is what the limit is actually for: the human-facing API surface, and
above all `/api/v1/auth/*` (login brute force, which additionally has its own
per-account limiter in routers/auth.py).

Why paths and not slowapi's own exemption
-----------------------------------------
slowapi exempts by *route handler name* (`limiter._exempt_routes`, filled by the
`@limiter.exempt` decorator) — that would mean touching dozens of endpoints and
remembering the decorator on every new one. `limiter.request_filter` callbacks
take no arguments and cannot see the request at all. A path check in the
middleware is the one place that covers every current and future route.
"""
from __future__ import annotations

from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Agent-container and internal machine traffic. `/api/v1/agent/` keeps the
# trailing slash on purpose: the human-facing `/api/v1/agents...` routes must
# stay limited.
EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/v1/agent/",
    "/api/v1/internal/",
)

# Liveness/readiness — probes and the frontend's health poll must never be able
# to burn a user's budget, and a rate-limited health check reads as "down".
EXEMPT_PATHS: tuple[str, ...] = (
    "/health",
    "/api/v1/health",
)

# Server-Sent-Events. A stream counts once per *connection*, but a flaky network
# reconnects them in bursts, and losing the event stream to a 429 silently
# freezes the UI instead of showing an error. Covers e.g.
# /api/v1/agents/{id}/chat/stream, /api/v1/activity/stream,
# /api/v1/boards/{id}/tasks/{id}/events.
EXEMPT_SUFFIXES: tuple[str, ...] = (
    "/stream",
    "/events",
)


def is_rate_limit_exempt(path: str) -> bool:
    """True for machine traffic, health checks and SSE streams."""
    if path in EXEMPT_PATHS:
        return True
    if path.startswith(EXEMPT_PREFIXES):
        return True
    if path.endswith(EXEMPT_SUFFIXES):
        return True
    return False


class PathExemptSlowAPIMiddleware(SlowAPIMiddleware):
    """SlowAPIMiddleware that skips the exempt paths above.

    Subclassing keeps slowapi's own behaviour (headers, exception handler,
    per-route decorators) intact for everything else — we only decide earlier
    whether a request is counted at all.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if is_rate_limit_exempt(request.url.path):
            return await call_next(request)
        return await super().dispatch(request, call_next)
