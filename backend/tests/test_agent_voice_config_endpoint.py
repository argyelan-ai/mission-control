"""GET /api/v1/agent/voice/config — which provider Jarvis is bound to (ADR-074).

This endpoint is the wire that makes a runtime switch actually reach the voice
container. Two properties carry the whole design and are asserted here:

1. It never fails hard. The worker calls it at the start of every call; a 500
   or a raised exception would end with Jarvis silent, which is strictly worse
   than Jarvis talking to yesterday's provider.
2. It never returns key material. The API keys live in the voice-worker's own
   env. MC neither stores nor forwards them — ADR-056 Finding 5 is the scar
   from a global key leaking into runtimes that had no business holding one.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _agent_with_runtime(runtime_id=None, **agent_kw) -> dict[str, str]:
    """Create a jarvis-ish agent (optionally bound) and return its auth header."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="Jarvis", slug=f"jarvis-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        agent_kw.setdefault("name", "Jarvis")
        agent_kw.setdefault("agent_runtime", "host")
        agent_kw.setdefault("harness", "jarvis")
        agent = Agent(
            role="assistant",
            scopes=["tasks:read"],
            board_id=board.id,
            agent_token_hash=token_hash,
            runtime_id=runtime_id,
            **agent_kw,
        )
        s.add(agent)
        await s.commit()

    return {"Authorization": f"Bearer {raw_token}"}


async def _runtime(runtime_type: str, **kw):
    from app.models.runtime import Runtime

    kw.setdefault("slug", f"rt-{uuid.uuid4().hex[:6]}")
    kw.setdefault("display_name", "Probe")
    kw.setdefault("model_identifier", "some-model")
    kw.setdefault("endpoint", "https://api.example.test")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = Runtime(runtime_type=runtime_type, **kw)
        s.add(rt)
        await s.commit()
        await s.refresh(rt)
    return rt


@pytest.mark.asyncio
async def test_bound_voice_runtime_is_reported(client: AsyncClient):
    rt = await _runtime(
        "voice_xai",
        slug="voice-xai",
        display_name="Jarvis Voice — Grok (xAI)",
        model_identifier="grok-voice-think-fast-1.0",
    )
    headers = await _agent_with_runtime(rt.id)

    resp = await client.get("/api/v1/agent/voice/config", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "xai"
    assert body["model"] == "grok-voice-think-fast-1.0"
    assert body["runtime_slug"] == "voice-xai"


@pytest.mark.asyncio
async def test_unbound_agent_gets_a_reason_not_an_error(client: AsyncClient):
    """Today's state: Jarvis has runtime_id NULL. Must be a calm 200."""
    headers = await _agent_with_runtime(None)

    resp = await client.get("/api/v1/agent/voice/config", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": False, "reason": "no_runtime_bound"}


@pytest.mark.asyncio
async def test_a_chat_runtime_is_refused(client: AsyncClient):
    """Belt and braces: is_compatible() should stop this at switch time, but if
    a binding ever gets through, the worker must not be handed 'vllm_docker'
    as a provider name."""
    rt = await _runtime("vllm_docker")
    headers = await _agent_with_runtime(rt.id)

    body = (await client.get("/api/v1/agent/voice/config", headers=headers)).json()

    assert body == {"ok": False, "reason": "not_a_voice_runtime"}


@pytest.mark.asyncio
async def test_disabled_runtime_is_refused(client: AsyncClient):
    """Disabling the row is Mark's documented rollback path — it has to work."""
    rt = await _runtime("voice_openai", enabled=False)
    headers = await _agent_with_runtime(rt.id)

    body = (await client.get("/api/v1/agent/voice/config", headers=headers)).json()

    assert body == {"ok": False, "reason": "runtime_disabled"}


@pytest.mark.asyncio
async def test_response_never_carries_key_material(client: AsyncClient):
    """The response is read by a process that already holds the keys. Sending
    them would put credentials on a wire that has no reason to carry them."""
    rt = await _runtime("voice_openai", slug="voice-openai")
    headers = await _agent_with_runtime(rt.id)

    raw = (await client.get("/api/v1/agent/voice/config", headers=headers)).text.lower()

    for forbidden in ("api_key", "apikey", "sk-", "secret", "token", "bearer"):
        assert forbidden not in raw, f"voice config leaked {forbidden!r}: {raw}"


@pytest.mark.asyncio
async def test_route_rejects_anonymous_callers(client: AsyncClient):
    """Without the agent token this would be an unauthenticated read of the
    fleet's provider configuration."""
    resp = await client.get("/api/v1/agent/voice/config")

    assert resp.status_code in (401, 403), resp.text
