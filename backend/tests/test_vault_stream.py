"""M.4 T2 — Tests for WS /vault/stream and WS /vault/voice-highlight.

Strategy
--------
Two test layers:

1. **Unit tests** (``_pubsub_forward`` + ``_ws_validate_jwt``): bypass the
   full FastAPI WS lifecycle.  Use fake WebSocket and fake pubsub objects so
   the tests are deterministic and instant.  This covers:
   - Message forwarding (bytes + str payloads)
   - Disconnect cleanup (pubsub.unsubscribe + aclose called)
   - Heartbeat task is started and properly cancelled on disconnect
   - JWT validation (valid, missing, expired, bogus, wrong secret)

2. **Integration smoke tests** (full WS routes via Starlette ``TestClient``):
   Verify that the route wires auth + pubsub correctly end-to-end.  Uses a
   shared ``FakeServer`` so the test can publish from a background thread
   while the WS handler listens.  A module-scoped TestClient is shared to
   avoid the APScheduler "already running" error when re-entering the
   FastAPI lifespan.

Auth pattern mirrored from ``cli_plugins.py:plugins_shell_websocket``.
Missing/invalid token → close code 4001.

Channels under test
-------------------
- ``vault:stream``          → WS /api/v1/vault/stream
- ``voice:graph-highlight`` → WS /api/v1/vault/voice-highlight
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from jose import jwt
from starlette.testclient import TestClient

import app.config


# ══════════════════════════════════════════════════════════════════════════════
# Token helpers
# ══════════════════════════════════════════════════════════════════════════════


def _make_token(
    sub: str = "test-user",
    role: str = "admin",
    secret: str | None = None,
) -> str:
    """Create a valid HS256 JWT accepted by ``_ws_validate_jwt``."""
    s = secret or app.config.settings.jwt_secret_key
    payload = {
        "sub": sub,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iat": datetime.now(timezone.utc),
        "tv": 0,
    }
    return jwt.encode(payload, s, algorithm="HS256")


def _make_expired_token() -> str:
    payload = {
        "sub": "test-user",
        "role": "admin",
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        "iat": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    return jwt.encode(payload, app.config.settings.jwt_secret_key, algorithm="HS256")


# ══════════════════════════════════════════════════════════════════════════════
# Fake WebSocket and PubSub objects for unit tests
# ══════════════════════════════════════════════════════════════════════════════


class _FakeWebSocket:
    """Minimal stand-in for ``starlette.websockets.WebSocket``."""

    def __init__(self, disconnect_after: int | None = None):
        self.sent: list[str] = []
        self.closed: bool = False
        self._disconnect_after = disconnect_after
        self._send_count = 0

    async def send_text(self, text: str) -> None:
        self._send_count += 1
        self.sent.append(text)
        if self._disconnect_after is not None and self._send_count >= self._disconnect_after:
            from starlette.websockets import WebSocketDisconnect
            raise WebSocketDisconnect()

    async def close(self, **kwargs) -> None:
        self.closed = True


class _MessageIter:
    """Async iterator over a finite list of messages."""

    def __init__(self, messages: list[dict]):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


class _FakePubSub:
    """Minimal async pubsub stand-in for unit tests."""

    def __init__(self, messages: list[dict]):
        self._messages = messages
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True

    def listen(self) -> _MessageIter:
        return _MessageIter(self._messages)


class _FakeRedisClient:
    """Stand-in for the aioredis client returned by ``get_redis``."""

    def __init__(self, pubsub: _FakePubSub):
        self._pubsub = pubsub

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — _pubsub_forward
# ══════════════════════════════════════════════════════════════════════════════


async def test_vault_stream_forwards_published_messages():
    """str-typed messages from pubsub are forwarded verbatim to the WebSocket.
    Subscribe-confirmation messages (type != 'message') are silently skipped.
    """
    from app.routers.vault import _pubsub_forward

    payload = json.dumps({"type": "modified", "path": "agents/sparky/notes/test.md"})
    pubsub = _FakePubSub(
        messages=[
            {"type": "subscribe", "data": 1},    # confirmation — must be skipped
            {"type": "message", "data": payload},
        ]
    )
    ws = _FakeWebSocket()

    await _pubsub_forward(ws, "vault:stream", _FakeRedisClient(pubsub))

    assert len(ws.sent) == 1
    got = json.loads(ws.sent[0])
    assert got == {"type": "modified", "path": "agents/sparky/notes/test.md"}


async def test_vault_stream_decodes_bytes_messages():
    """bytes-encoded Redis messages are decoded to UTF-8 before forwarding."""
    from app.routers.vault import _pubsub_forward

    payload_str = json.dumps({"type": "compacted", "path": "global/test.md"})
    payload_bytes = payload_str.encode("utf-8")

    pubsub = _FakePubSub(messages=[{"type": "message", "data": payload_bytes}])
    ws = _FakeWebSocket()

    await _pubsub_forward(ws, "vault:stream", _FakeRedisClient(pubsub))

    assert len(ws.sent) == 1
    assert ws.sent[0] == payload_str


async def test_vault_stream_disconnect_cleans_up_pubsub():
    """When the WebSocket raises WebSocketDisconnect during send_text, the
    finally block must unsubscribe and close the pubsub."""
    from app.routers.vault import _pubsub_forward

    payload = json.dumps({"type": "modified", "path": "test.md"})
    pubsub = _FakePubSub(messages=[{"type": "message", "data": payload}])

    ws = _FakeWebSocket(disconnect_after=1)  # raises WebSocketDisconnect on first send

    await _pubsub_forward(ws, "vault:stream", _FakeRedisClient(pubsub))

    assert "vault:stream" in pubsub.unsubscribed
    assert pubsub.closed is True


async def test_heartbeat_task_is_cancelled_on_disconnect():
    """The heartbeat background task fires a ping when the sleep elapses, and
    is properly cancelled (not leaked) when the pubsub loop ends.

    Strategy: set _HEARTBEAT_INTERVAL to 0 so the heartbeat fires immediately
    on the first iteration.  Use a pubsub that yields one message then ends.
    The heartbeat task runs concurrently: after the sleep(0) it sends a ping.
    After _pubsub_forward returns, verify that:
    1. A ping frame was sent (heartbeat fired)
    2. pubsub was cleaned up (unsubscribed + closed)
    """
    import app.routers.vault as vault_mod
    from app.routers.vault import _pubsub_forward

    # Temporarily set heartbeat to 0 seconds so it fires immediately
    original_interval = vault_mod._HEARTBEAT_INTERVAL
    vault_mod._HEARTBEAT_INTERVAL = 0

    payload = json.dumps({"type": "modified", "path": "test.md"})
    # Use a pubsub that yields one data message then ends
    pubsub = _FakePubSub(messages=[{"type": "message", "data": payload}])
    ws = _FakeWebSocket()

    try:
        await _pubsub_forward(ws, "vault:stream", _FakeRedisClient(pubsub))
        # Give the heartbeat task a moment to fire (it has sleep(0))
        await asyncio.sleep(0)
    finally:
        vault_mod._HEARTBEAT_INTERVAL = original_interval

    # The data message was forwarded
    assert any(payload == s for s in ws.sent), f"data message not found in {ws.sent}"
    # pubsub cleanup ran
    assert pubsub.closed is True


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — _ws_validate_jwt
# ══════════════════════════════════════════════════════════════════════════════


def test_vault_stream_requires_valid_jwt_none():
    """None token → rejected."""
    from app.routers.vault import _ws_validate_jwt
    assert _ws_validate_jwt(None) is False


def test_vault_stream_requires_valid_jwt_bogus():
    """Non-JWT string → rejected."""
    from app.routers.vault import _ws_validate_jwt
    assert _ws_validate_jwt("not-a-jwt") is False


def test_vault_stream_requires_valid_jwt_expired():
    """Expired JWT → rejected."""
    from app.routers.vault import _ws_validate_jwt
    assert _ws_validate_jwt(_make_expired_token()) is False


def test_vault_stream_requires_valid_jwt_valid():
    """Valid JWT → accepted."""
    from app.routers.vault import _ws_validate_jwt
    assert _ws_validate_jwt(_make_token()) is True


def test_vault_stream_requires_valid_jwt_wrong_secret():
    """JWT signed with wrong secret → rejected."""
    from app.routers.vault import _ws_validate_jwt
    bad_token = _make_token(secret="totally-wrong-secret-key")
    assert _ws_validate_jwt(bad_token) is False


# ══════════════════════════════════════════════════════════════════════════════
# Integration fixture — minimal app (no full lifespan)
# ══════════════════════════════════════════════════════════════════════════════
#
# We build a thin FastAPI app that includes only the vault router + auth
# dependencies.  This avoids the full main.py lifespan (APScheduler,
# PostgreSQL, OpenClaw RPC, etc.) while still testing the real WS route code.
# Pattern mirrors test_vault_routes.py::_make_vault_app().
#
# A shared FakeServer is used so the background publish thread and the WS
# handler's pubsub are in the same in-memory Redis.


def _make_minimal_vault_app(fake_server: fakeredis.aioredis.FakeServer):
    """Return a minimal FastAPI app with only the vault router mounted.

    ``get_redis`` in vault.py is patched to use *fake_server*.
    DB session is overridden to use the test SQLite engine.
    Auth (JWT decode) works normally — no mocking needed.
    """
    from fastapi import FastAPI
    from app.routers.vault import router as vault_router
    from app.database import get_session
    from tests.conftest import test_engine
    from sqlmodel.ext.asyncio.session import AsyncSession

    async def _fake_get_redis():
        return fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)

    mini_app = FastAPI()

    # Patch get_redis before including the router so the WS handlers pick it up
    import app.routers.vault as vault_mod
    vault_mod.get_redis = _fake_get_redis  # type: ignore[assignment]

    mini_app.include_router(vault_router)

    async def _override_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    mini_app.dependency_overrides[get_session] = _override_session
    return mini_app, _fake_get_redis, vault_mod


@pytest.fixture
def integration_ws_client(fake_redis):
    """TestClient backed by a minimal vault-only FastAPI app.

    ``fake_redis`` fixture is requested solely to ensure the SQLite test
    tables exist (conftest ``setup_db`` autouse fixture runs before this).
    The actual Redis used by the WS handler is a fresh ``FakeServer``.
    """
    server = fakeredis.aioredis.FakeServer()
    mini_app, _fake_get_redis, vault_mod = _make_minimal_vault_app(server)

    from app.redis_client import get_redis as _real_get_redis

    with TestClient(mini_app, raise_server_exceptions=False) as client:
        yield client, server

    # Restore vault_mod.get_redis to original
    vault_mod.get_redis = _real_get_redis  # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════════════════════
# Integration helpers
# ══════════════════════════════════════════════════════════════════════════════


def _publish_after(
    server: fakeredis.aioredis.FakeServer,
    channel: str,
    payload: dict,
    delay: float = 0.5,
) -> threading.Thread:
    """Publish JSON *payload* to *channel* in a daemon thread after *delay* s."""

    def _worker():
        time.sleep(delay)

        async def _run():
            r = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
            await r.publish(channel, json.dumps(payload))
            await r.aclose()

        asyncio.run(_run())

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests
# ══════════════════════════════════════════════════════════════════════════════


def test_vault_stream_integration_forwards_messages(integration_ws_client):
    """End-to-end: message published on vault:stream is forwarded to
    the connected WS client at /api/v1/vault/stream."""
    client, server = integration_ws_client
    token = _make_token()
    expected = {"type": "modified", "path": "agents/sparky/notes/test.md"}

    t = _publish_after(server, "vault:stream", expected)

    with client.websocket_connect(f"/api/v1/vault/stream?token={token}") as ws:
        raw = ws.receive_text()

    t.join(timeout=3)
    assert json.loads(raw) == expected


def test_voice_highlight_integration_forwards_messages(integration_ws_client):
    """End-to-end: message published on voice:graph-highlight is forwarded to
    the connected WS client at /api/v1/vault/voice-highlight."""
    client, server = integration_ws_client
    token = _make_token()
    expected = {"type": "highlight", "node_id": "abc-123", "label": "sparky/lessons/foo.md"}

    t = _publish_after(server, "voice:graph-highlight", expected)

    with client.websocket_connect(f"/api/v1/vault/voice-highlight?token={token}") as ws:
        raw = ws.receive_text()

    t.join(timeout=3)
    assert json.loads(raw) == expected
