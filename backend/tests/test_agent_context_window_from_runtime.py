"""The agent's context window comes from its runtime — the same number the chat uses.

Round-2 user test (t4 S-neu-3): a local agent's API said ``context_max: 200000``
while its runtime served 1,048,576 tokens, and the chat ring and the agent
page disagreed. The chat already divides by the bound runtime's window
(``runtime_window.runtime_context_window`` — rule R-runtime-window-one-source);
the agent row kept the default it was created with.

The heartbeat is the one place that writes the agent's context figures, so it
now brings ``agent.context_max`` in line with the runtime window before it
turns the reported percentage into tokens. Agents whose vendor knows the
window (Claude Code, Grok, Kimi) keep their own number — MC never tells those
harnesses a window, so the runtime row is not their truth.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _agent_on_runtime(*, runtime_type: str, slug: str, window: int | None, tokens: int | None = None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.runtime import Runtime

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rt = Runtime(
            slug=slug,
            display_name=slug,
            runtime_type=runtime_type,
            endpoint="http://box.example.com:8000/v1",
            enabled=True,
            max_context_len=window,
        )
        s.add(rt)
        await s.commit()
        await s.refresh(rt)
        agent = Agent(
            id=uuid.uuid4(),
            name=f"ctx-{slug}",
            agent_runtime="cli-bridge",
            status="idle",
            runtime_id=rt.id,
            context_max=200_000,
            context_tokens=tokens,
            agent_token_hash=token_hash,
            scopes=["heartbeat", "tasks:read"],
        )
        s.add(agent)
        await s.commit()
    return agent.id, raw_token


async def _heartbeat(client: AsyncClient, token: str, **body):
    resp = await client.post(
        "/api/v1/agent/me/heartbeat",
        json={"status": "idle", **body},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


async def _fresh(agent_id):
    from app.models.agent import Agent

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(Agent, agent_id)


@pytest.mark.asyncio
async def test_heartbeat_sizes_the_agent_to_its_runtime_window(client: AsyncClient):
    agent_id, token = await _agent_on_runtime(
        runtime_type="openai_compatible", slug="ctx-local", window=1_048_576
    )

    await _heartbeat(client, token, context_pct=2.0)

    agent = await _fresh(agent_id)
    assert agent.context_max == 1_048_576
    # 2 % of the REAL window — the same tokens the chat ring shows.
    assert agent.context_tokens == round(0.02 * 1_048_576)


@pytest.mark.asyncio
async def test_heartbeat_without_a_reading_still_fixes_the_window(client: AsyncClient):
    """Hermes sends no percentage on some paths — its window must not stay
    at the creation default anyway (the page divides by it)."""
    agent_id, token = await _agent_on_runtime(
        runtime_type="hermes", slug="ctx-hermes", window=1_048_576
    )

    await _heartbeat(client, token)

    assert (await _fresh(agent_id)).context_max == 1_048_576


@pytest.mark.asyncio
async def test_vendor_window_agents_keep_their_own_number(client: AsyncClient):
    """Claude Code is never told the runtime row's window (internal.py
    _context_window_env only runs for OpenAI-protocol harnesses)."""
    agent_id, token = await _agent_on_runtime(
        runtime_type="anthropic_oauth", slug="anthropic-claude-ctx", window=1_000_000
    )

    await _heartbeat(client, token, context_pct=10.0)

    agent = await _fresh(agent_id)
    assert agent.context_max == 200_000
    assert agent.context_tokens == 20_000


@pytest.mark.asyncio
async def test_runtime_without_a_window_keeps_the_agent_default(client: AsyncClient):
    agent_id, token = await _agent_on_runtime(
        runtime_type="openai_compatible", slug="ctx-unknown", window=None
    )

    await _heartbeat(client, token, context_pct=50.0)

    agent = await _fresh(agent_id)
    assert agent.context_max == 200_000
    assert agent.context_tokens == 100_000


@pytest.mark.asyncio
async def test_a_window_change_keeps_the_last_reported_percent(client: AsyncClient):
    """The stored tokens came from a percent against the old window; until the
    next reading the page must keep that percent, not shrink it 5x."""
    agent_id, token = await _agent_on_runtime(
        runtime_type="openai_compatible", slug="ctx-rescale", window=1_000_000, tokens=20_000
    )

    await _heartbeat(client, token)  # no reading this cycle

    agent = await _fresh(agent_id)
    assert agent.context_max == 1_000_000
    assert agent.context_tokens == 100_000  # still 10 %


# ── Read time, not only heartbeat time (round-3 user test, r1 NICE-1) ────────
# The window was only brought in line on a heartbeat. An agent that has not
# beaten since its runtime changed (Rex: 131,072 on a 1,048,576 runtime, last
# heartbeat three weeks old) kept the stale window on its page. The API now
# derives the shown window from the runtime when it is READ.


@pytest.mark.asyncio
async def test_agent_detail_shows_the_runtime_window_without_a_heartbeat(auth_client: AsyncClient):
    agent_id, _ = await _agent_on_runtime(
        runtime_type="openai_compatible", slug="ctx-read", window=1_000_000, tokens=20_000
    )

    resp = await auth_client.get(f"/api/v1/agents/{agent_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["context_max"] == 1_000_000
    assert body["context_tokens"] == 100_000  # the stored 10 % is kept


@pytest.mark.asyncio
async def test_agent_list_shows_the_runtime_window_without_a_heartbeat(auth_client: AsyncClient):
    agent_id, _ = await _agent_on_runtime(
        runtime_type="openai_compatible", slug="ctx-list", window=1_000_000, tokens=40_000
    )

    resp = await auth_client.get("/api/v1/agents")

    assert resp.status_code == 200, resp.text
    row = next(a for a in resp.json() if a["id"] == str(agent_id))
    assert row["context_max"] == 1_000_000
    assert row["context_tokens"] == 200_000  # the stored 20 % is kept


def test_shown_context_keeps_unknown_tokens_unknown():
    from app.services.runtime_window import shown_context

    assert shown_context(None, 200_000, 1_000_000) == (None, 1_000_000)
    assert shown_context(20_000, 200_000, None) == (20_000, 200_000)
    assert shown_context(20_000, 200_000, 200_000) == (20_000, 200_000)


@pytest.mark.asyncio
async def test_read_time_leaves_vendor_window_agents_alone(auth_client: AsyncClient):
    agent_id, _ = await _agent_on_runtime(
        runtime_type="anthropic_oauth", slug="anthropic-claude-read", window=1_000_000, tokens=20_000
    )

    body = (await auth_client.get(f"/api/v1/agents/{agent_id}")).json()

    assert body["context_max"] == 200_000
    assert body["context_tokens"] == 20_000


@pytest.mark.asyncio
async def test_read_time_does_not_write_the_row(auth_client: AsyncClient):
    """A GET is a read: the stored figures change on the next heartbeat only."""
    agent_id, _ = await _agent_on_runtime(
        runtime_type="openai_compatible", slug="ctx-nowrite", window=1_000_000, tokens=20_000
    )

    await auth_client.get(f"/api/v1/agents/{agent_id}")

    agent = await _fresh(agent_id)
    assert agent.context_max == 200_000
    assert agent.context_tokens == 20_000
