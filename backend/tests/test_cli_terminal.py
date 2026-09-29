"""Tests for /api/v1/agents/{id}/cli-sessions and terminal endpoints."""
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
import app.routers.cli_terminal as cli_mod

from tests.conftest import test_engine


MOCK_SESSIONS = [
    {"task_id": "abc12345", "session": "freecode-abc12345", "elapsed_seconds": 60}
]

_FAKE_AGENT = Agent(
    id=uuid.UUID(int=42),
    name="FreeCode Test Agent",
    agent_runtime="cli-bridge",
)


@pytest.mark.anyio
async def test_cli_sessions_returns_list(auth_client: AsyncClient):
    """GET /cli-sessions returns bridge sessions."""
    from app.main import app as fastapi_app

    async def override_cli_agent():
        return _FAKE_AGENT

    fastapi_app.dependency_overrides[cli_mod._get_cli_agent] = override_cli_agent

    try:
        with patch("app.routers.cli_terminal._bridge_get") as mock_get:
            mock_get.return_value = MOCK_SESSIONS
            resp = await auth_client.get(
                f"/api/v1/agents/{_FAKE_AGENT.id}/cli-sessions",
            )
    finally:
        fastapi_app.dependency_overrides.pop(cli_mod._get_cli_agent, None)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["task_id"] == "abc12345"


@pytest.mark.anyio
async def test_cli_sessions_requires_auth(client: AsyncClient):
    """GET /cli-sessions without token -> 401."""
    resp = await client.get(
        f"/api/v1/agents/{_FAKE_AGENT.id}/cli-sessions"
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_cli_input_sends_to_bridge(auth_client: AsyncClient):
    """POST /terminal/{task_id}/input sends text to bridge."""
    from app.main import app as fastapi_app

    task_id = str(uuid.uuid4())

    async def override_cli_agent():
        return _FAKE_AGENT

    fastapi_app.dependency_overrides[cli_mod._get_cli_agent] = override_cli_agent

    try:
        with patch("app.routers.cli_terminal._bridge_post") as mock_post:
            mock_post.return_value = {"ok": True}
            resp = await auth_client.post(
                f"/api/v1/agents/{_FAKE_AGENT.id}/terminal/{task_id}/input",
                json={"text": "/compact"},
            )
    finally:
        fastapi_app.dependency_overrides.pop(cli_mod._get_cli_agent, None)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.anyio
async def test_cli_kill_session(auth_client: AsyncClient):
    """DELETE /terminal/{task_id} ends bridge session."""
    from app.main import app as fastapi_app

    task_id = str(uuid.uuid4())

    async def override_cli_agent():
        return _FAKE_AGENT

    fastapi_app.dependency_overrides[cli_mod._get_cli_agent] = override_cli_agent

    try:
        with patch("app.routers.cli_terminal._bridge_delete") as mock_delete:
            mock_delete.return_value = {"ok": True}
            resp = await auth_client.delete(
                f"/api/v1/agents/{_FAKE_AGENT.id}/terminal/{task_id}",
            )
    finally:
        fastapi_app.dependency_overrides.pop(cli_mod._get_cli_agent, None)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.anyio
async def test_cli_input_requires_text(auth_client: AsyncClient):
    """POST /terminal/{task_id}/input with empty text -> 400."""
    from app.main import app as fastapi_app

    task_id = str(uuid.uuid4())

    async def override_cli_agent():
        return _FAKE_AGENT

    fastapi_app.dependency_overrides[cli_mod._get_cli_agent] = override_cli_agent

    try:
        resp = await auth_client.post(
            f"/api/v1/agents/{_FAKE_AGENT.id}/terminal/{task_id}/input",
            json={"text": ""},
        )
    finally:
        fastapi_app.dependency_overrides.pop(cli_mod._get_cli_agent, None)

    assert resp.status_code == 400


# ── Tests for GET /api/v1/agents/{id}/cli-sessions (agent-scoped) ────────────


@pytest.mark.anyio
async def test_cli_sessions_bridge_unavailable(auth_client: AsyncClient):
    """GET /cli-sessions returns [] when bridge is unreachable."""
    from app.main import app as fastapi_app

    async def override_cli_agent():
        return _FAKE_AGENT

    fastapi_app.dependency_overrides[cli_mod._get_cli_agent] = override_cli_agent

    try:
        with patch("app.routers.cli_terminal._bridge_get") as mock_get:
            mock_get.return_value = None
            resp = await auth_client.get(
                f"/api/v1/agents/{_FAKE_AGENT.id}/cli-sessions",
            )
    finally:
        fastapi_app.dependency_overrides.pop(cli_mod._get_cli_agent, None)

    assert resp.status_code == 200
    assert resp.json() == []
