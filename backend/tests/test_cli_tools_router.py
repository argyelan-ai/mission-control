"""CLI Tools API router (CLI-Tool-Updates, Task 7).

Auth matrix (viewer/operator), 404/409 on update, happy-path list/update,
/check triggering an on-demand run_check_once, and update-status idle vs
in-flight.
"""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.redis_client import RedisKeys
from app.services.cli_update_runner import UnknownTool, UpdateAlreadyRunning
from tests.conftest import test_engine


async def _viewer_token() -> str:
    """JWT for a viewer user (pattern from test_hosts_api._viewer_token)."""
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            User(
                id=uid,
                email=f"viewer-{uid.hex[:8]}@mc.local",
                name="Viewer",
                role="viewer",
                is_active=True,
            )
        )
        await s.commit()
    return create_access_token(str(uid), "viewer")


_CACHE_FIXTURE = {
    "openclaude": {
        "installed": "1.0.0", "target": "1.0.0", "latest": "1.1.0",
        "update_available": True, "checked_at": "2026-07-05T00:00:00+00:00",
    },
    "claude": {
        "installed": "2.0.0", "target": "2.0.0", "latest": "2.0.0",
        "update_available": False, "checked_at": "2026-07-05T00:00:00+00:00",
    },
    "omp": {
        "installed": "3.0.0", "target": "3.0.0", "latest": "3.0.0",
        "update_available": False, "checked_at": "2026-07-05T00:00:00+00:00",
    },
}


async def _seed_cache(fake_redis) -> None:
    await fake_redis.set(RedisKeys.cli_versions_cache(), json.dumps(_CACHE_FIXTURE))


# ── Auth matrix ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_viewer_can_read_list_and_status(client, fake_redis):
    await _seed_cache(fake_redis)
    token = await _viewer_token()
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.get("/api/v1/cli-tools")
    assert resp.status_code == 200
    resp = await client.get("/api/v1/cli-tools/update-status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_viewer_forbidden_from_check_and_update(client, fake_redis):
    token = await _viewer_token()
    client.headers["Authorization"] = f"Bearer {token}"

    assert (await client.post("/api/v1/cli-tools/check")).status_code == 403
    assert (await client.post("/api/v1/cli-tools/claude/update")).status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_401(client):
    assert (await client.get("/api/v1/cli-tools")).status_code == 401
    assert (await client.post("/api/v1/cli-tools/check")).status_code == 401


# ── List ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_cached_versions_and_agents_affected(
    async_session, auth_client, fake_redis, make_agent
):
    await _seed_cache(fake_redis)
    agent = await make_agent(
        "Sparky", agent_runtime="cli-bridge", harness="claude", current_task_id=None
    )

    resp = await auth_client.get("/api/v1/cli-tools")
    assert resp.status_code == 200
    data = resp.json()
    tools = {t["tool"]: t for t in data["tools"]}
    assert set(tools) == {"openclaude", "claude", "omp"}

    claude = tools["claude"]
    assert claude["installed"] == "2.0.0"
    assert claude["update_available"] is False
    assert claude["image"] == "mc-claude-agent:latest"
    assert [a["id"] for a in claude["agents_affected"]] == [str(agent.id)]
    assert claude["agents_affected"][0]["busy"] is False

    openclaude = tools["openclaude"]
    assert openclaude["update_available"] is True
    assert openclaude["agents_affected"] == []


@pytest.mark.asyncio
async def test_list_excludes_host_agents_and_flags_busy(
    async_session, auth_client, fake_redis, make_agent
):
    await _seed_cache(fake_redis)
    await make_agent("Boss", agent_runtime="host", harness="claude")
    busy_task_id = uuid.uuid4()
    await make_agent(
        "Rex", agent_runtime="cli-bridge", harness="claude", current_task_id=busy_task_id
    )

    resp = await auth_client.get("/api/v1/cli-tools")
    claude = next(t for t in resp.json()["tools"] if t["tool"] == "claude")
    names = {a["id"]: a["busy"] for a in claude["agents_affected"]}
    assert len(names) == 1
    assert list(names.values()) == [True]


@pytest.mark.asyncio
async def test_list_empty_cache_triggers_on_demand_check(async_session, auth_client, fake_redis):
    """No cache yet → GET falls back to run_check_once() so the cockpit
    never shows a blank first load."""
    with patch(
        "app.routers.cli_tools.cli_update_check.run_check_once",
        AsyncMock(return_value=_CACHE_FIXTURE),
    ) as mock_check:
        resp = await auth_client.get("/api/v1/cli-tools")
    assert resp.status_code == 200
    mock_check.assert_awaited_once()
    tools = {t["tool"]: t for t in resp.json()["tools"]}
    assert tools["claude"]["installed"] == "2.0.0"


@pytest.mark.asyncio
async def test_list_build_state_from_progress(async_session, auth_client, fake_redis):
    await _seed_cache(fake_redis)
    await fake_redis.set(
        RedisKeys.cli_update_progress(),
        json.dumps({"phase": "build", "tool": "claude", "from_version": "2.0.0", "to_version": "2.1.0"}),
    )
    resp = await auth_client.get("/api/v1/cli-tools")
    tools = {t["tool"]: t for t in resp.json()["tools"]}
    assert tools["claude"]["build_state"] == "build"
    assert tools["openclaude"]["build_state"] is None


# ── /check ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_triggers_run_check_once(async_session, auth_client, fake_redis):
    with patch(
        "app.routers.cli_tools.cli_update_check.run_check_once",
        AsyncMock(return_value=_CACHE_FIXTURE),
    ) as mock_check:
        resp = await auth_client.post("/api/v1/cli-tools/check")
    assert resp.status_code == 200
    mock_check.assert_awaited_once()
    assert resp.json()["tools"]["claude"]["installed"] == "2.0.0"


# ── /update-status ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_status_idle_when_no_progress(auth_client, fake_redis):
    resp = await auth_client.get("/api/v1/cli-tools/update-status")
    assert resp.status_code == 200
    assert resp.json() == {"phase": "idle"}


@pytest.mark.asyncio
async def test_update_status_reflects_progress(auth_client, fake_redis):
    payload = {"phase": "build", "tool": "omp", "from_version": "3.0.0", "to_version": "3.1.0"}
    await fake_redis.set(RedisKeys.cli_update_progress(), json.dumps(payload))
    resp = await auth_client.get("/api/v1/cli-tools/update-status")
    assert resp.status_code == 200
    assert resp.json() == payload


# ── POST /{tool}/update ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_unknown_tool_404(auth_client, fake_redis):
    with patch(
        "app.routers.cli_tools.start_update", AsyncMock(side_effect=UnknownTool("bogus"))
    ):
        resp = await auth_client.post("/api/v1/cli-tools/bogus/update")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_already_running_409(auth_client, fake_redis):
    with patch(
        "app.routers.cli_tools.start_update",
        AsyncMock(side_effect=UpdateAlreadyRunning()),
    ):
        resp = await auth_client.post("/api/v1/cli-tools/claude/update")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_happy_path_202(auth_client, fake_redis):
    with patch(
        "app.routers.cli_tools.start_update", AsyncMock(return_value="tok-123")
    ) as mock_start:
        resp = await auth_client.post("/api/v1/cli-tools/claude/update")
    assert resp.status_code == 202
    assert resp.json() == {"status": "started"}
    mock_start.assert_awaited_once_with("claude")
