"""SlowAPIMiddleware must actually enforce default_limits.

Regression guard for PR #404 (Rex review, MEDIUM): `Limiter(default_limits=
["120/minute"])` was built and `app.state.limiter` was set, but no
`SlowAPIMiddleware` and no `@limiter.limit` decorator existed anywhere in
the repo — `default_limits` without either is inert, so there was in fact
no global rate limiting despite the code reading as if there were.

This asserts a real HTTP 429, not just that the Limiter object exists.
Uses a throwaway low-limit Limiter swapped onto `app.state.limiter` for the
duration of the test so it doesn't depend on — or pollute — the shared
120/minute budget other tests may also consume via the same in-process app.
"""
import pytest
from httpx import AsyncClient
from slowapi import Limiter
from slowapi.util import get_remote_address

import app.main as main_module


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
        resp = await client.get("/health")
        statuses.append(resp.status_code)

    assert 429 in statuses, (
        f"expected a 429 among {statuses} once the 3/minute default limit "
        "was exceeded — SlowAPIMiddleware is not enforcing default_limits"
    )
    # And the request(s) before the limit kicked in must have gone through.
    assert 200 in statuses
