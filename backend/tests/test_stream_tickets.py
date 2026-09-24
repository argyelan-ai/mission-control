"""Stream tickets: SSE/WebSocket connections authenticate with a short-lived,
single-use, path-bound ticket instead of the login JWT in the URL.

Finding (24.09.2026): the UI opened every EventSource with
``?token=<login JWT>``; the reverse proxy wrote those URIs into its error log
(hundreds of lines a day) — anyone able to read container logs could take
over the operator session. See app/services/stream_tickets.py.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from jose import jwt
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient

import app.config
from app.auth import create_access_token
from app.services import stream_tickets
from tests.conftest import test_engine

STREAM = "/api/v1/activity/stream"
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")  # auth_client's admin


@pytest.fixture
def stub_activity_stream(monkeypatch):
    """The real /activity/stream never ends; the auth dependency runs before
    the body, so a stub response is enough to observe accept/reject."""
    import app.routers.activity as activity

    monkeypatch.setattr(
        activity, "make_sse_response", lambda channels: JSONResponse({"opened": True})
    )


async def _issue(client, path: str = STREAM) -> dict:
    r = await client.post("/api/v1/auth/stream-ticket", json={"path": path})
    assert r.status_code == 200, r.text
    return r.json()


# ── Issuing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_returns_opaque_high_entropy_ticket(auth_client):
    body = await _issue(auth_client)
    ticket = body["ticket"]
    # token_urlsafe(32) → 256 bit → 43 url-safe chars; floor is 128 bit.
    assert len(ticket) >= 22
    assert "." not in ticket, "must be opaque, not a JWT"
    assert body["expires_in"] == app.config.settings.stream_ticket_ttl_seconds == 60
    # Two tickets never collide.
    assert (await _issue(auth_client))["ticket"] != ticket


@pytest.mark.asyncio
async def test_issue_requires_authentication(client):
    r = await client.post("/api/v1/auth/stream-ticket", json={"path": STREAM})
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/tasks",                      # REST route, not a stream
        "/api/v1/auth/me",
        "/api/v1/agents/stream?x=1",          # query string not allowed
        "https://evil.example/api/v1/agents/stream",
        "/api/v1/../v1/agents/stream",
        "/elsewhere/stream",
    ],
)
async def test_issue_refuses_non_stream_paths(auth_client, path):
    r = await auth_client.post("/api/v1/auth/stream-ticket", json={"path": path})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_ticket_is_stored_hashed_with_ttl(auth_client, fake_redis):
    ticket = (await _issue(auth_client))["ticket"]
    keys = [k async for k in fake_redis.scan_iter("mc:stream-ticket:*")]
    assert len(keys) == 1
    assert ticket not in keys[0], "raw ticket must not be the Redis key"
    ttl = await fake_redis.ttl(keys[0])
    assert 0 < ttl <= 60


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/agents/stream",
        "/api/v1/agents/5b1d2c3e-0000-4000-8000-000000000001/chat/stream",
        "/api/v1/agents/5b1d2c3e-0000-4000-8000-000000000001/terminal-events/stream",
        "/api/v1/boards/5b1d2c3e-0000-4000-8000-000000000001/tasks/stream",
        "/api/v1/groups/5b1d2c3e-0000-4000-8000-000000000001/stream",
        "/api/v1/schedule/stream",
        "/api/v1/vault/stream",
        "/api/v1/vault/voice-highlight",
        "/api/v1/vault/voice-display",
        "/api/v1/plugins/shell/ws",
        "/api/v1/browser-live/ws",
        "/api/v1/agents/5b1d2c3e-0000-4000-8000-000000000001/terminal",
        "/api/v1/agents/5b1d2c3e-0000-4000-8000-000000000001/terminal/ws",
        "/api/v1/host-agents/5b1d2c3e-0000-4000-8000-000000000001/terminal",
    ],
)
def test_every_browser_stream_path_is_ticketable(path):
    assert stream_tickets.is_stream_path(path)


# ── Redeeming on a real stream endpoint ──────────────────────────────────


@pytest.mark.asyncio
async def test_stream_opens_with_ticket(auth_client, stub_activity_stream):
    ticket = (await _issue(auth_client))["ticket"]
    auth_client.headers.pop("Authorization")
    r = await auth_client.get(f"{STREAM}?ticket={ticket}")
    assert r.status_code == 200
    assert r.json() == {"opened": True}


@pytest.mark.asyncio
async def test_ticket_is_single_use(auth_client, stub_activity_stream):
    ticket = (await _issue(auth_client))["ticket"]
    auth_client.headers.pop("Authorization")
    assert (await auth_client.get(f"{STREAM}?ticket={ticket}")).status_code == 200
    assert (await auth_client.get(f"{STREAM}?ticket={ticket}")).status_code == 401


@pytest.mark.asyncio
async def test_ticket_for_another_path_is_rejected_and_burnt(auth_client, stub_activity_stream):
    ticket = (await _issue(auth_client, "/api/v1/agents/stream"))["ticket"]
    auth_client.headers.pop("Authorization")
    assert (await auth_client.get(f"{STREAM}?ticket={ticket}")).status_code == 401
    # Burnt: not even usable on its own path afterwards.
    claims = await stream_tickets.redeem_ticket(ticket, "/api/v1/agents/stream")
    assert claims is None


@pytest.mark.asyncio
async def test_expired_ticket_is_rejected(auth_client, stub_activity_stream, monkeypatch):
    monkeypatch.setattr(app.config.settings, "stream_ticket_ttl_seconds", 1)
    ticket = (await _issue(auth_client))["ticket"]
    auth_client.headers.pop("Authorization")
    await asyncio.sleep(1.2)
    assert (await auth_client.get(f"{STREAM}?ticket={ticket}")).status_code == 401


@pytest.mark.asyncio
async def test_garbage_ticket_is_rejected(client, stub_activity_stream):
    assert (await client.get(f"{STREAM}?ticket=not-a-ticket")).status_code == 401


# ── Bound to the user ─────────────────────────────────────────────────────


async def _make_user(**kw):
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        user = User(id=uid, email=f"{uid}@mc.local", name="U", role="operator",
                    is_active=True, **kw)
        s.add(user)
        await s.commit()
    return uid


@pytest.mark.asyncio
async def test_ticket_resolves_to_its_issuer_not_another_user(client):
    from app.auth import _user_from_stream_ticket

    a = await _make_user()
    b = await _make_user()
    ticket = await stream_tickets.issue_ticket(user_id=str(a), token_version=0, path=STREAM)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        user = await _user_from_stream_ticket(STREAM, ticket, s)
    assert user.id == a != b


@pytest.mark.asyncio
async def test_ticket_dies_with_logout(auth_client, stub_activity_stream):
    """A ticket minted before logout (token_version bump) must not open a
    stream afterwards."""
    ticket = (await _issue(auth_client))["ticket"]
    assert (await auth_client.post("/api/v1/auth/logout")).status_code == 200
    auth_client.headers.pop("Authorization")
    assert (await auth_client.get(f"{STREAM}?ticket={ticket}")).status_code == 401


@pytest.mark.asyncio
async def test_ticket_of_deactivated_or_unknown_user_is_rejected(client, stub_activity_stream):
    from app.models.user import User

    uid = await _make_user()
    ticket = await stream_tickets.issue_ticket(user_id=str(uid), token_version=0, path=STREAM)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        user = await s.get(User, uid)
        user.is_active = False
        s.add(user)
        await s.commit()
    assert (await client.get(f"{STREAM}?ticket={ticket}")).status_code == 401

    ghost = await stream_tickets.issue_ticket(
        user_id=str(uuid.uuid4()), token_version=0, path=STREAM
    )
    assert (await client.get(f"{STREAM}?ticket={ghost}")).status_code == 401


# ── The login JWT in the URL ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jwt_in_query_is_rejected_by_default(client, stub_activity_stream):
    assert app.config.settings.allow_query_token_auth is False
    token = create_access_token(str(USER_ID), "admin")
    from app.models.user import User

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=USER_ID, email="q@mc.local", name="Q", role="admin", is_active=True))
        await s.commit()
    r = await client.get(f"{STREAM}?token={token}")
    assert r.status_code == 401
    # Same token in the header still works — only the URL channel is closed.
    r = await client.get(STREAM, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_jwt_in_query_works_only_with_explicit_fallback(client, stub_activity_stream, monkeypatch):
    from app.models.user import User

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=USER_ID, email="q@mc.local", name="Q", role="admin", is_active=True))
        await s.commit()
    token = create_access_token(str(USER_ID), "admin")
    monkeypatch.setattr(app.config.settings, "allow_query_token_auth", True)
    assert (await client.get(f"{STREAM}?token={token}")).status_code == 200


# ── WebSockets ────────────────────────────────────────────────────────────


@pytest.fixture
def ws_probe(monkeypatch):
    """Mini app with one WebSocket guarded by authenticate_websocket, and a
    ticket store that works across the TestClient's event loop."""
    from app.auth import authenticate_websocket

    server = fakeredis.aioredis.FakeServer()

    async def _redis():
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    monkeypatch.setattr(stream_tickets, "get_redis", _redis)

    mini = FastAPI()

    @mini.websocket("/api/v1/probe/ws")
    async def probe(websocket: WebSocket, token: str | None = None, ticket: str | None = None):
        if not await authenticate_websocket(websocket, token=token, ticket=ticket):
            await websocket.close(code=4001)
            return
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    def issue(path="/api/v1/probe/ws"):
        return asyncio.run(
            stream_tickets.issue_ticket(user_id=str(USER_ID), token_version=0, path=path)
        )

    with TestClient(mini) as c:
        yield c, issue


def _opens(client, url) -> bool:
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(url) as ws:
            return ws.receive_text() == "ok"
    except WebSocketDisconnect:
        return False


def _jwt(**extra) -> str:
    payload = {
        "sub": str(USER_ID), "role": "admin", "tv": 0,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1), **extra,
    }
    return jwt.encode(payload, app.config.settings.jwt_secret_key, algorithm="HS256")


def test_ws_opens_with_ticket_once(ws_probe):
    client, issue = ws_probe
    ticket = issue()
    assert _opens(client, f"/api/v1/probe/ws?ticket={ticket}")
    assert not _opens(client, f"/api/v1/probe/ws?ticket={ticket}")


def test_ws_rejects_ticket_for_other_path(ws_probe):
    client, issue = ws_probe
    assert not _opens(client, f"/api/v1/probe/ws?ticket={issue('/api/v1/vault/stream')}")


def test_ws_rejects_jwt_in_query_by_default(ws_probe):
    client, _ = ws_probe
    assert not _opens(client, f"/api/v1/probe/ws?token={_jwt()}")


def test_ws_query_jwt_fallback_never_accepts_scoped_tokens(ws_probe, monkeypatch):
    """A bench view link token (scope claim, shareable by design) must never
    open a terminal/shell WebSocket — the old per-router checks only looked
    at `sub` and accepted it."""
    client, _ = ws_probe
    monkeypatch.setattr(app.config.settings, "allow_query_token_auth", True)
    assert _opens(client, f"/api/v1/probe/ws?token={_jwt()}")
    assert not _opens(client, f"/api/v1/probe/ws?token={_jwt(scope='bench_view')}")


# ── Logs ──────────────────────────────────────────────────────────────────


def test_ticket_query_param_is_redacted_in_logs():
    from app.log_redaction import redact_secrets

    line = 'GET /api/v1/agents/stream?ticket=AbC123-_xyz&since=4 HTTP/1.1" 200'
    out = redact_secrets(line)
    assert "AbC123" not in out
    assert "since=4" in out
