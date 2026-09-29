"""Agent-scoping hardening (scoping finding 2026-09-15, three points).

Contract guarantees under test — no abuse recipe, only observable behavior:

1. Legacy non-UUID-sub admin fallback (require_user / require_user_or_agent):
   resolves ONLY for the allow-listed host service identity ("mcp-server",
   issued by scripts/mc-mcp.py). Any other non-UUID sub carrying an admin-role
   claim is rejected with 401 — an admin-role claim alone must not resolve to
   a real admin account.
2. The 401 hint for agent tokens on user routes names an agent-scoped route
   ONLY where one actually exists; for user routes without an agent-scoped
   counterpart it says so instead of pointing the caller at a 404.

Positive paths that must keep working (Gegenrichtung):
- a real user JWT (UUID sub) passes require_user;
- the host service identity passes require_user AND require_user_or_agent;
- a real agent token passes require_agent on /agent/me;
- where an agent-scoped counterpart exists, the pointed hint still appears.
"""

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from tests.conftest import test_engine

# conftest.test_engine — direct DB seeding into the same SQLite as the app.


async def _seed_admin_user() -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.user import User

        s.add(
            User(
                id=user_id,
                email=f"admin-{uuid.uuid4().hex[:6]}@mc.local",
                name="Admin",
                role="admin",
                is_active=True,
            )
        )
        await s.commit()
    return user_id


async def _seed_agent_with_token() -> tuple[uuid.UUID, str]:
    from app.models.agent import Agent

    raw_token, token_hash = generate_agent_token()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            Agent(
                id=agent_id,
                name=f"Agent-{uuid.uuid4().hex[:6]}",
                agent_runtime="cli-bridge",
                agent_token_hash=token_hash,
            )
        )
        await s.commit()
    return agent_id, raw_token


# ── Punkt 1: non-UUID admin fallback narrowed to the service allowlist ──────


@pytest.mark.asyncio
async def test_known_service_sub_passes_require_user(client):
    """Gegenrichtung: the host MCP server identity keeps working."""
    await _seed_admin_user()
    token = create_access_token("mcp-server", "admin")
    resp = await client.get(
        "/api/v1/repos", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unknown_non_uuid_admin_sub_rejected(client):
    """A non-UUID sub outside the allowlist must not resolve to an admin."""
    await _seed_admin_user()
    token = create_access_token("some-legacy-service", "admin")
    resp = await client.get(
        "/api/v1/repos", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_real_user_uuid_sub_still_passes(client):
    """Gegenrichtung: ordinary user login JWT unaffected."""
    user_id = await _seed_admin_user()
    token = create_access_token(str(user_id), "admin")
    resp = await client.get(
        "/api/v1/repos", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_known_service_sub_passes_user_or_agent(client):
    """Gegenrichtung: control-plane routes keep accepting the service identity."""
    await _seed_admin_user()
    token = create_access_token("mcp-server", "admin")
    resp = await client.post(
        f"/api/v1/agents/{uuid.uuid4()}/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Auth passed → the deprecated route itself answers (410 Gone), not 401.
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_unknown_non_uuid_admin_sub_rejected_on_user_or_agent(client):
    await _seed_admin_user()
    token = create_access_token("some-legacy-service", "admin")
    resp = await client.post(
        f"/api/v1/agents/{uuid.uuid4()}/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


# ── Punkt 3: agent-token 401 hint only points at existing agent routes ──────


@pytest.mark.asyncio
async def test_agent_token_hint_without_agent_counterpart_does_not_invent_route(client):
    """User routes without an agent-scoped counterpart must not be answered
    with a pointer to a nonexistent agent route."""
    _, agent_token = await _seed_agent_with_token()
    resp = await client.patch(
        f"/api/v1/repos/{uuid.uuid4()}",
        json={"description": "x"},
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert resp.status_code == 401
    detail = resp.json().get("detail", "")
    assert "/api/v1/agent/repos" not in detail


@pytest.mark.asyncio
async def test_agent_token_hint_with_agent_counterpart_still_points(client):
    """Gegenrichtung: where an agent-scoped counterpart exists, the hint
    still names it."""
    _, agent_token = await _seed_agent_with_token()
    resp = await client.get(
        "/api/v1/agents", headers={"Authorization": f"Bearer {agent_token}"}
    )
    assert resp.status_code == 401
    detail = resp.json().get("detail", "")
    assert "/api/v1/agent/agents" in detail


@pytest.mark.asyncio
async def test_agent_token_on_agent_route_still_passes(client):
    """Gegenrichtung: real agent token keeps working on agent-scoped routes."""
    _, agent_token = await _seed_agent_with_token()
    resp = await client.get(
        "/api/v1/agent/me", headers={"Authorization": f"Bearer {agent_token}"}
    )
    assert resp.status_code == 200
