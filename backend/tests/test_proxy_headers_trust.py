"""Regression guard for the proxy-headers trust fix (PR #404 Rex review,
MEDIUM/LOW): without `--proxy-headers --forwarded-allow-ips=*` on uvicorn
(backend/Dockerfile), `request.client.host` is always the Caddy container's
IP, never the real client. Both the login rate limiter
(routers/auth.py::login) and slowapi's `get_remote_address` key on
`request.client.host` — with the wrong value, every user shares one bucket
(unauthenticated remote DoS on login) and per-attacker limiting does nothing.

uvicorn's ProxyHeadersMiddleware is applied by the uvicorn CLI/Config layer
(the `--proxy-headers` flag), not registered via `app.add_middleware` — it
never wraps `app.main:app` when tests import it directly via
httpx.ASGITransport. This test exercises the exact middleware + trust
config (`trusted_hosts="*"`) shipped in the Dockerfile CMD directly,
independent of the full app, to prove the mechanism the Dockerfile flag
relies on actually does what the fix assumes.
"""
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


@pytest.mark.asyncio
async def test_wildcard_trust_maps_x_forwarded_for_to_real_client():
    captured: dict = {}

    async def inner_app(scope, receive, send):
        captured["client"] = scope.get("client")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = ProxyHeadersMiddleware(inner_app, trusted_hosts="*")

    scope = {
        "type": "http",
        # Caddy's container IP changes on every recreate — this stands in
        # for whatever it happens to be right now.
        "client": ("172.20.0.5", 54321),
        "headers": [(b"x-forwarded-for", b"203.0.113.7")],
    }

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    await wrapped(scope, receive, send)

    assert captured["client"] is not None
    assert captured["client"][0] == "203.0.113.7", (
        f"expected the real client IP from X-Forwarded-For, got "
        f"{captured['client']!r} (still Caddy's own address) — "
        "trusted_hosts='*' is not rewriting scope['client']"
    )


@pytest.mark.asyncio
async def test_without_proxy_headers_trust_client_is_the_proxy():
    """Sanity check: this is the broken pre-fix behavior — proves the test
    above is actually exercising the trust boundary, not a no-op."""
    captured: dict = {}

    async def inner_app(scope, receive, send):
        captured["client"] = scope.get("client")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    # uvicorn's own default when --proxy-headers is passed without
    # --forwarded-allow-ips: only 127.0.0.1 is trusted, so a container IP
    # like Caddy's is NOT trusted and the header is ignored.
    wrapped = ProxyHeadersMiddleware(inner_app, trusted_hosts="127.0.0.1")

    scope = {
        "type": "http",
        "client": ("172.20.0.5", 54321),
        "headers": [(b"x-forwarded-for", b"203.0.113.7")],
    }

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    await wrapped(scope, receive, send)

    assert captured["client"][0] == "172.20.0.5", (
        "expected Caddy's own IP to survive untouched when it isn't in "
        "the trusted set — this is the bug PR #404 found"
    )
