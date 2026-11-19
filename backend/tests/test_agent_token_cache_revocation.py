"""A replaced agent token stops working at once — the token cache must not keep it alive.

Found 2026-11-13: the Redis cache maps SHA256(token) → agent id (ADR-010).
A cache hit returned the agent whenever it still had *any* token hash, so
after `POST /agents/{id}/reset-token` the OLD token was accepted until the
entry expired (up to five minutes). Reset is the only way to cut an agent's
access for good, so "reset" has to mean "gone now".

The cache entry is now bound to the token hash it was verified against.
"""
import uuid

import pytest

from app.auth import (
    _agent_token_cache_key,
    _resolve_agent_from_token,
    generate_agent_token,
)
from app.models.agent import Agent


async def _agent_with_token(session, name: str = "CacheBot") -> tuple[Agent, str]:
    raw_token, token_hash = generate_agent_token()
    agent = Agent(id=uuid.uuid4(), name=name, agent_runtime="cli-bridge", agent_token_hash=token_hash)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


@pytest.fixture
def count_pbkdf2(monkeypatch):
    """Counts the expensive verifications — the reason the cache exists."""
    import app.auth as auth_mod

    calls = {"n": 0}
    original = auth_mod.verify_agent_token

    def counting(token: str, token_hash: str) -> bool:
        calls["n"] += 1
        return original(token, token_hash)

    monkeypatch.setattr(auth_mod, "verify_agent_token", counting)
    return calls


@pytest.mark.asyncio
async def test_old_token_is_refused_right_after_the_hash_was_replaced(session, isolate_redis_singleton):
    agent, old_token = await _agent_with_token(session)

    # First request: verified with PBKDF2 and cached.
    assert (await _resolve_agent_from_token(old_token, session)).id == agent.id
    assert await isolate_redis_singleton.get(_agent_token_cache_key(old_token)) is not None

    # Token reset: the hash is replaced, the cache entry of the old token stays.
    new_token, new_hash = generate_agent_token()
    agent.agent_token_hash = new_hash
    session.add(agent)
    await session.commit()

    assert await _resolve_agent_from_token(old_token, session) is None
    # The stale entry is gone, not just ignored.
    assert await isolate_redis_singleton.get(_agent_token_cache_key(old_token)) is None
    # The new token works.
    assert (await _resolve_agent_from_token(new_token, session)).id == agent.id


@pytest.mark.asyncio
async def test_cache_still_saves_the_expensive_check(session, isolate_redis_singleton, count_pbkdf2):
    """The fix must not cost the cache its purpose (ADR-010)."""
    agent, token = await _agent_with_token(session)

    assert (await _resolve_agent_from_token(token, session)).id == agent.id
    after_first = count_pbkdf2["n"]
    assert after_first >= 1

    for _ in range(3):
        assert (await _resolve_agent_from_token(token, session)).id == agent.id
    assert count_pbkdf2["n"] == after_first


@pytest.mark.asyncio
async def test_entry_in_the_old_format_is_checked_again(session, isolate_redis_singleton, count_pbkdf2):
    """Entries written before this change hold only the agent id. They are
    not trusted: the token is verified again and the entry rewritten."""
    agent, token = await _agent_with_token(session)
    key = _agent_token_cache_key(token)
    await isolate_redis_singleton.set(key, str(agent.id), ex=300)

    assert (await _resolve_agent_from_token(token, session)).id == agent.id
    assert count_pbkdf2["n"] >= 1
    assert await isolate_redis_singleton.get(key) != str(agent.id)


@pytest.mark.asyncio
async def test_old_format_entry_does_not_let_a_replaced_token_in(session, isolate_redis_singleton):
    """The deploy window: an old-format entry of a token that was reset."""
    agent, old_token = await _agent_with_token(session)
    await isolate_redis_singleton.set(_agent_token_cache_key(old_token), str(agent.id), ex=300)

    _, new_hash = generate_agent_token()
    agent.agent_token_hash = new_hash
    session.add(agent)
    await session.commit()

    assert await _resolve_agent_from_token(old_token, session) is None


@pytest.mark.asyncio
async def test_cache_holds_neither_the_token_nor_its_stored_hash(session, isolate_redis_singleton):
    """ADR-010: Redis never holds the token, and not the PBKDF2 hash either."""
    agent, token = await _agent_with_token(session)
    await _resolve_agent_from_token(token, session)

    value = await isolate_redis_singleton.get(_agent_token_cache_key(token))
    assert token not in value
    assert agent.agent_token_hash not in value
    _, derived_key = agent.agent_token_hash.split(":", 1)
    assert derived_key not in value


@pytest.mark.asyncio
async def test_reset_token_route_locks_the_old_token_out_at_once(auth_client):
    """End to end: create → use (cached) → reset → the old token gets 401."""
    created = await auth_client.post(
        "/api/v1/agents",
        json={"name": "ResetBot", "agent_runtime": "manual"},
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    old_token = created.json()["token"]

    first = await auth_client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {old_token}"})
    assert first.status_code == 200, first.text

    reset = await auth_client.post(f"/api/v1/agents/{agent_id}/reset-token")
    assert reset.status_code == 200, reset.text
    new_token = reset.json()["token"]

    stale = await auth_client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {old_token}"})
    assert stale.status_code == 401, stale.text

    fresh = await auth_client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {new_token}"})
    assert fresh.status_code == 200, fresh.text
