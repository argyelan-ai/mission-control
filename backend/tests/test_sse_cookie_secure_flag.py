"""Regression tests for the conditional Secure flag on the mc_sse_token
session cookie (follow-up finding from PR #404).

mc_sse_token is NOT an SSE detail — `require_user` (app/auth.py) falls back
to it for EVERY route when no Bearer header is present, making it a full
session credential. It must therefore carry `Secure` whenever the request
arrived over TLS and must NOT carry it on plain HTTP (localhost / LAN /
Tailscale-without-TLS), or the browser drops the cookie and locks the user
out of their own UI.

The TLS signal is `request.url.scheme` (ASGI scope["scheme"]): behind Caddy
uvicorn runs with --proxy-headers --forwarded-allow-ips=* (backend/Dockerfile,
PR #404), so ProxyHeadersMiddleware rewrites the scheme from
X-Forwarded-Proto of the trusted proxy — no blind secure=True.

httpx.ASGITransport derives scope["scheme"] from the client's base_url, so
the HTTPS case uses an https:// base_url and the plain-HTTP case the shared
http:// client fixture.
"""

import uuid

import httpx
import pytest

from app.auth import hash_password


@pytest.fixture
async def login_user():
    """A user with a known password for POST /api/v1/auth/login."""
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.user import User
    from tests.conftest import test_engine

    email = f"secure-flag-{uuid.uuid4().hex[:8]}@mc.local"
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            User(
                id=uuid.uuid4(),
                email=email,
                name="Secure Flag Tester",
                password_hash=hash_password("correct-horse"),
                role="admin",
                is_active=True,
            )
        )
        await s.commit()

    return {"email": email, "password": "correct-horse"}


def _sse_cookie(resp: httpx.Response) -> str:
    """The raw Set-Cookie header for mc_sse_token."""
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("mc_sse_token="):
            return value
    raise AssertionError(f"login did not set mc_sse_token: {resp.headers}")


def _cookie_parts(cookie: str) -> list[str]:
    return [p.strip().lower() for p in cookie.split(";")]


@pytest.mark.asyncio
async def test_login_over_tls_sets_secure_flag(client, login_user):
    """HTTPS request (scheme https, as ProxyHeadersMiddleware derives it from
    a trusted proxy's X-Forwarded-Proto: https behind Caddy) → the
    mc_sse_token cookie carries the Secure attribute."""
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as https_client:
        resp = await https_client.post(
            "/api/v1/auth/login",
            json={"email": login_user["email"], "password": login_user["password"]},
        )
    assert resp.status_code == 200, resp.text

    cookie = _sse_cookie(resp)
    assert "secure" in _cookie_parts(cookie), cookie


@pytest.mark.asyncio
async def test_login_over_plain_http_omits_secure_flag(client, login_user):
    """Plain HTTP login (localhost / LAN / Tailscale without TLS) → NO Secure
    attribute; otherwise the browser drops the cookie and locks the user out."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": login_user["email"], "password": login_user["password"]},
    )
    assert resp.status_code == 200, resp.text

    cookie = _sse_cookie(resp)
    assert "secure" not in _cookie_parts(cookie), cookie

    # Session stays intact over plain HTTP: the cookie authenticates a
    # require_user route (full session credential, not SSE-only).
    token = cookie.split(";")[0].split("=", 1)[1]
    me = await client.get(
        "/api/v1/auth/me", headers={"Cookie": f"mc_sse_token={token}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == login_user["email"]
