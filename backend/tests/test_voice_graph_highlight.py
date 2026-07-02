"""Tests for POST /api/v1/voice/graph-highlight (M.4 T5).

Voice → Redis → Frontend bridge: voice worker publishes filter commands to the
`voice:graph-highlight` Redis channel, the /vault/voice-highlight WS endpoint
(M.4 T2) forwards them verbatim to connected clients.

Auth fixture pattern mirrors test_vault_routes_agent_search.py:
- Minimal FastAPI app with voice.router
- Real agent with PBKDF2 token + explicit scopes
- get_redis dependency override with fakeredis
- AsyncClient with Bearer token
"""

import asyncio
import json
import uuid

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── App factory ───────────────────────────────────────────────────────────────


def _make_voice_app(fake_redis) -> FastAPI:
    """Minimal FastAPI app with voice router + DB session override + redis override."""
    from app.database import get_session
    from app.redis_client import get_redis
    from app.routers.voice import router as voice_router

    fa = FastAPI()
    fa.include_router(voice_router)

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    async def override_get_redis():
        return fake_redis

    fa.dependency_overrides[get_session] = override_get_session
    fa.dependency_overrides[get_redis] = override_get_redis

    # The route resolves get_redis() via direct module call, NOT only via
    # Depends. Patch the module attribute too so the published message is
    # captured by our fake.
    import app.routers.voice as voice_mod

    voice_mod.get_redis = override_get_redis  # type: ignore[assignment]
    return fa


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def fake_redis_local():
    """Per-test fakeredis with pub/sub support."""
    server = fakeredis.aioredis.FakeServer()
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    yield redis
    await redis.aclose()


async def _make_agent_with_scopes(name: str, scopes: list[str]) -> tuple[str, uuid.UUID]:
    """Create an agent with given scopes; return (raw_token, agent_id)."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw_token, token_hash = generate_agent_token()
    agent_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=agent_id,
            name=name,
            role="voice",
            agent_token_hash=token_hash,
            scopes=scopes,
        )
        s.add(agent)
        await s.commit()

    return raw_token, agent_id


async def _drain_channel(redis, channel: str, timeout: float = 1.0) -> list[str]:
    """Subscribe to a Redis channel and collect all messages published until timeout."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    messages: list[str] = []
    try:
        # Drain the initial subscribe-ack message
        for _ in range(5):  # bounded poll loop
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            if msg is None:
                break
            if msg.get("type") == "message":
                data = msg.get("data")
                messages.append(data if isinstance(data, str) else data.decode())
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
    return messages


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publishes_filter_to_redis(fake_redis_local):
    """Valid POST publishes a JSON message to voice:graph-highlight channel."""
    from app.scopes import Scope

    raw_token, _ = await _make_agent_with_scopes(
        name="Jarvis",
        scopes=[Scope.VAULT_READ.value],
    )

    app_instance = _make_voice_app(fake_redis_local)

    # Pre-subscribe before issuing the POST so we don't miss the publish.
    pubsub = fake_redis_local.pubsub()
    await pubsub.subscribe("voice:graph-highlight")
    # Eat the subscribe-ack so the next get_message sees the real publish.
    await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)

    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.post(
            "/api/v1/voice/graph-highlight",
            json={"filter": {"agent": "sparky", "type": "lesson"}},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["published_at"].endswith("Z")

    # Pull the message from the pubsub queue.
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
    await pubsub.unsubscribe("voice:graph-highlight")
    await pubsub.aclose()

    assert msg is not None, "Redis publish was not received on voice:graph-highlight"
    payload = json.loads(msg["data"] if isinstance(msg["data"], str) else msg["data"].decode())
    assert payload["filter"] == {"agent": "sparky", "type": "lesson"}
    assert payload["requested_by"] == "jarvis"
    assert payload["requested_at"].endswith("Z")


@pytest.mark.asyncio
async def test_rejects_unknown_filter_keys(fake_redis_local):
    """Unknown filter keys (e.g. typos) → 422 with validator error."""
    from app.scopes import Scope

    raw_token, _ = await _make_agent_with_scopes(
        name="Jarvis",
        scopes=[Scope.VAULT_READ.value],
    )

    app_instance = _make_voice_app(fake_redis_local)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.post(
            "/api/v1/voice/graph-highlight",
            json={"filter": {"fitler": "typo"}},
        )

    assert r.status_code == 422, r.text
    body = r.json()
    detail_str = json.dumps(body)
    assert "unknown filter keys" in detail_str


@pytest.mark.asyncio
async def test_rejects_non_str_values(fake_redis_local):
    """filter[k] must be str or list[str]; ints are rejected (422)."""
    from app.scopes import Scope

    raw_token, _ = await _make_agent_with_scopes(
        name="Jarvis",
        scopes=[Scope.VAULT_READ.value],
    )

    app_instance = _make_voice_app(fake_redis_local)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.post(
            "/api/v1/voice/graph-highlight",
            json={"filter": {"agent": 123}},
        )

    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_requires_vault_read_scope(fake_redis_local):
    """Agent without vault:read scope → 403."""
    from app.scopes import Scope

    raw_token, _ = await _make_agent_with_scopes(
        name="ScopelessJarvis",
        # HEARTBEAT only — non-empty list so backward-compat (empty=ALL) is NOT triggered
        scopes=[Scope.HEARTBEAT.value],
    )

    app_instance = _make_voice_app(fake_redis_local)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.post(
            "/api/v1/voice/graph-highlight",
            json={"filter": {"agent": "sparky"}},
        )

    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_filter_can_be_list_for_multi_select(fake_redis_local):
    """filter values may be list[str] for OR-match across multiple agents/types."""
    from app.scopes import Scope

    raw_token, _ = await _make_agent_with_scopes(
        name="Jarvis",
        scopes=[Scope.VAULT_READ.value],
    )

    app_instance = _make_voice_app(fake_redis_local)

    pubsub = fake_redis_local.pubsub()
    await pubsub.subscribe("voice:graph-highlight")
    await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)

    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.post(
            "/api/v1/voice/graph-highlight",
            json={"filter": {"agent": ["sparky", "cody"], "type": "lesson"}},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
    await pubsub.unsubscribe("voice:graph-highlight")
    await pubsub.aclose()

    assert msg is not None
    payload = json.loads(msg["data"] if isinstance(msg["data"], str) else msg["data"].decode())
    assert payload["filter"]["agent"] == ["sparky", "cody"]
    assert payload["filter"]["type"] == "lesson"
