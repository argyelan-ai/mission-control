"""CTX-01 (Phase 6): heartbeat self-reports context_pct → context_tokens.

Plan 06-02 backend half. Docker claude-binary agents scrape ctx% from tmux
statusline (poll.sh — Plan 06-03) and POST it via /api/v1/agent/me/heartbeat.
The handler inverts the display formula at routers/agents.py:166 to derive
context_tokens = round(context_pct/100 * agent.context_max).

Threat model:
- T-06-02-01 (Tampering): pydantic Field(ge=0, le=100) rejects out-of-range
  to prevent forced-compaction attacks.
- T-06-02-04 (NaN/Negative): pydantic float type + ge=0 rejects.

Wave-0 stubs from Plan 06-00 flip XFAIL → PASS via this plan.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_agent(session: AsyncSession, *, context_max: int = 200_000):
    """Create a cli-bridge agent with heartbeat scope and a known context_max."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name=f"CtxPctAgent-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        status="idle",
        agent_token_hash=token_hash,
        scopes=["heartbeat", "tasks:read"],
        context_max=context_max,
        context_tokens=0,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


def test_agent_heartbeat_payload_accepts_context_pct_field():
    """AgentHeartbeatPayload(context_pct=42.5) parses without error."""
    from app.routers.agents import AgentHeartbeatPayload

    payload = AgentHeartbeatPayload(context_pct=42.5)
    assert payload.context_pct == 42.5

    # Default = None (backward compat — existing callers don't break)
    payload_default = AgentHeartbeatPayload()
    assert payload_default.context_pct is None


@pytest.mark.asyncio
async def test_agent_heartbeat_handler_writes_context_tokens_from_pct(client: AsyncClient):
    """POST with context_pct=50 → context_tokens = round(50/100 * context_max)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent, token = await _create_agent(s, context_max=200_000)

    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "context_pct": 50.0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh = await s.get(Agent, agent.id)

    # round(50/100 * 200_000) = 100_000
    assert fresh.context_tokens == 100_000

    # Backward-compat: omitting context_pct preserves existing value.
    resp2 = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.agent import Agent
        fresh2 = await s.get(Agent, agent.id)

    # No context_pct in payload → context_tokens stays at 100_000.
    assert fresh2.context_tokens == 100_000


@pytest.mark.asyncio
async def test_agent_heartbeat_validates_context_pct_is_0_to_100_range(client: AsyncClient):
    """Out-of-range context_pct (>100, <0) rejected with HTTP 422."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        _, token = await _create_agent(s, context_max=200_000)

    # Above range: 150.0
    resp_high = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "context_pct": 150.0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_high.status_code == 422, resp_high.text

    # Below range: -5.0
    resp_low = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "context_pct": -5.0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_low.status_code == 422, resp_low.text

    # Boundary inclusive: 0 and 100 both accepted (Field(ge=0, le=100)).
    resp_zero = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle", "context_pct": 0.0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_zero.status_code == 200

    resp_full = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "working", "context_pct": 100.0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_full.status_code == 200
