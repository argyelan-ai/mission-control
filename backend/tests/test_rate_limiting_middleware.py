"""The rate-limit middleware must enforce default_limits — and must leave the
machine traffic alone.

Part 1 (PR #404, Rex review, MEDIUM): `Limiter(default_limits=["120/minute"])`
was built and `app.state.limiter` was set, but no `SlowAPIMiddleware` and no
`@limiter.limit` decorator existed anywhere in the repo — `default_limits`
without either is inert, so there was in fact no global rate limiting despite
the code reading as if there were.

Part 2 (PR #404 Nachlese, MITTEL-1): the counting key is the client IP. Marks
browser is one IP for the whole UI and the agent fleet polls every 5s, so the
limit now sits at 600/minute and `PathExemptSlowAPIMiddleware` takes agent
traffic, `/api/v1/internal/*`, `/health` and SSE streams out of the count
entirely (app/rate_limit.py). If that exemption ever silently stops working,
a polling fleet can lock Mark out of his own UI.

Both parts assert real HTTP status codes, not that objects exist.
"""
import pytest
from httpx import AsyncClient
from slowapi import Limiter
from slowapi.util import get_remote_address

import app.main as main_module
from app.rate_limit import is_rate_limit_exempt

# A route that is matched by the router (so the middleware counts it) and is
# NOT on the exempt list. It answers 401 without a token — that is fine here:
# the middleware runs before the auth dependency, so a 401 proves the request
# passed the limiter, and a 429 proves the limiter stopped it.
COUNTED_ROUTE = "/api/v1/system/version"


@pytest.fixture
def tiny_rate_limit():
    """Swap app.state.limiter for one allowing only 3 requests/minute.

    SlowAPIMiddleware reads app.state.limiter fresh on every request (see
    slowapi/middleware.py), so swapping the attribute is enough — no need
    to touch the middleware stack itself.
    """
    original = main_module.app.state.limiter
    main_module.app.state.limiter = Limiter(
        key_func=get_remote_address, default_limits=["3/minute"]
    )
    try:
        yield
    finally:
        main_module.app.state.limiter = original


@pytest.mark.asyncio
async def test_default_limit_is_enforced_by_middleware(
    client: AsyncClient, tiny_rate_limit
):
    statuses = []
    for _ in range(5):
        resp = await client.get(COUNTED_ROUTE)
        statuses.append(resp.status_code)

    assert 429 in statuses, (
        f"expected a 429 among {statuses} once the 3/minute default limit "
        "was exceeded — the middleware is not enforcing default_limits"
    )
    # And the request(s) before the limit kicked in must have reached the route.
    assert 401 in statuses, (
        f"expected the first requests in {statuses} to reach the endpoint "
        "(401 = past the limiter, stopped by auth)"
    )


@pytest.mark.asyncio
async def test_still_counts_when_slowapi_cannot_resolve_the_handler(
    client: AsyncClient, tiny_rate_limit, monkeypatch
):
    """The silent-inert case, pinned down.

    slowapi decides "count or not" by looking up the route handler in
    app.routes and reading its `.endpoint`. FastAPI 0.141 wraps included
    routers in objects that have no `.endpoint`, the lookup returns None, and
    slowapi treats "handler unknown" as "exempt" — measured: EVERY route was
    exempt and nothing was limited at all, on an app that looks fully
    protected. requirements.lock still pins 0.133, so this would have stayed
    invisible until the next dependency bump.

    Here we force the lookup to fail on the pinned version too, so the
    fallback in PathExemptSlowAPIMiddleware stays covered either way.
    """
    monkeypatch.setattr(
        "app.rate_limit._find_route_handler", lambda routes, scope: None
    )

    statuses = []
    for _ in range(5):
        resp = await client.get(COUNTED_ROUTE)
        statuses.append(resp.status_code)

    assert 429 in statuses, (
        f"expected a 429 among {statuses} — with an unresolvable route handler "
        "the middleware fell back to slowapi's 'unknown means exempt' and "
        "silently stopped rate limiting everything"
    )


@pytest.mark.asyncio
async def test_exempt_paths_stay_exempt_without_a_resolvable_handler(
    client: AsyncClient, tiny_rate_limit, monkeypatch
):
    """And the exemption must survive that same fallback path."""
    monkeypatch.setattr(
        "app.rate_limit._find_route_handler", lambda routes, scope: None
    )

    statuses = [(await client.get("/health")).status_code for _ in range(10)]

    assert 429 not in statuses, f"/health got rate limited ({statuses})"


@pytest.mark.asyncio
async def test_health_is_never_rate_limited(client: AsyncClient, tiny_rate_limit):
    """MITTEL-1: /health is a liveness probe. A 429 there reads as an outage,
    and the frontend polls it. 10 calls against a 3/minute budget."""
    statuses = [(await client.get("/health")).status_code for _ in range(10)]

    assert 429 not in statuses, (
        f"/health got rate limited ({statuses}) — the path exemption in "
        "app/rate_limit.py is not applied"
    )
    assert statuses.count(200) == 10


@pytest.mark.asyncio
async def test_agent_routes_are_never_rate_limited(
    client: AsyncClient, tiny_rate_limit
):
    """MITTEL-1: 15 agent containers poll /api/v1/agent/* every 5s straight at
    backend:8000. They must not be able to exhaust anyone's budget, and must
    not lose their poll to a 429."""
    statuses = [
        (await client.get("/api/v1/agent/me/poll")).status_code for _ in range(10)
    ]

    assert 429 not in statuses, (
        f"agent polling got rate limited ({statuses}) — the fleet would go "
        "silent while looking online"
    )


def test_exempt_path_matrix():
    """The exact rule, so a later refactor cannot quietly widen or narrow it."""
    # Machine traffic, liveness, SSE — never counted.
    assert is_rate_limit_exempt("/health")
    assert is_rate_limit_exempt("/api/v1/agent/me/poll")
    assert is_rate_limit_exempt("/api/v1/agent/vault/keys")
    assert is_rate_limit_exempt("/api/v1/internal/bootstrap")
    assert is_rate_limit_exempt("/api/v1/activity/stream")
    assert is_rate_limit_exempt("/api/v1/agents/abc/chat/stream")
    assert is_rate_limit_exempt("/api/v1/boards/b1/tasks/t1/events")

    # The human-facing surface stays limited — above all login brute force.
    assert not is_rate_limit_exempt("/api/v1/auth/login")
    assert not is_rate_limit_exempt("/api/v1/agents")
    assert not is_rate_limit_exempt("/api/v1/agents/abc")
    assert not is_rate_limit_exempt("/api/v1/tasks")
    assert not is_rate_limit_exempt("/api/v1/system/version")


def test_login_stays_rate_limited_in_the_shipped_config():
    """Sabotage guard: the exemption must never grow to cover /api/v1/auth/*.

    Login brute force is the whole reason the global limiter exists.
    """
    from app.rate_limit import EXEMPT_PATHS, EXEMPT_PREFIXES, EXEMPT_SUFFIXES

    assert not any("/auth" in p for p in EXEMPT_PREFIXES + EXEMPT_PATHS)
    assert not any(s in ("/login", "/token") for s in EXEMPT_SUFFIXES)


def test_shipped_default_limit_has_headroom_for_one_browser():
    """MITTEL-1: 120/minute was one browser's polling budget. Whatever the
    number becomes, it must stay clearly above that."""
    groups = main_module.limiter._default_limits
    assert groups, "default_limits disappeared — global rate limiting would be inert"

    # A LimitGroup iterates into the concrete Limit objects ("600 per 1 minute").
    limits = [str(limit.limit) for group in groups for limit in group]
    assert any("600 per 1 minute" in text for text in limits), (
        f"expected the 600/minute default budget, found {limits}"
    )
